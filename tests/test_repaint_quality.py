import pytest

from local_music_engine.auto_quality import quality_policy
from local_music_engine.storage import ProjectStore
from local_music_engine.views import library
from local_music_engine.workflow import generate_candidates, repaint_candidate, review_candidate, select_candidate
from test_auto_quality import LYRICS, Reports, generate
from test_workflow import FakeAceClient


def test_repaint_checks_once_keeps_range_and_preserves_human_selection(tmp_path):
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    store = ProjectStore.initialize(tmp_path / "song", title="편집", lyrics=LYRICS,
                                    style_prompt="Korean acoustic pop", target_duration_seconds=10)
    parent = generate_candidates(store.root, seeds=[1], client_factory=FakeAceClient, engine="ace-step")["candidateIds"][0]
    select_candidate(store.root, parent)
    review_candidate(store.root, parent, status="approved")
    backend = Reports(["바다는 차갑고 어둠은 깊게 번진다"])
    result = repaint_candidate(store.root, start_seconds=2, end_seconds=5, seed=8,
        client_factory=FakeAceClient, quality=quality_policy(max_attempts=1), quality_backend=backend, engine="ace-step")
    project = store.load()
    raw, finished = project["candidates"][-2:]
    assert FakeAceClient.submitted == [1, 8] and backend.calls == 1
    assert raw["parentCandidateId"] == parent and finished["parentCandidateId"] == raw["candidateId"]
    assert raw["editRange"] == finished["editRange"] == {"startSeconds": 2.0, "endSeconds": 5.0}
    assert raw["quality"]["preferred"] is False and finished["quality"]["preferred"] is True
    assert finished["quality"]["status"] == "attention" and finished["quality"]["maxAttempts"] == 1
    assert result["candidateId"] == result["recommendedCandidateId"] == finished["candidateId"]
    assert project["selectedCandidateId"] == parent and project["recommendedCandidateId"] == finished["candidateId"]
    assert finished["humanReview"]["status"] == raw["humanReview"]["status"] == "unreviewed"
    assert all(store.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    request = store.find_by_id(project, "requests", "requestId", finished["requestId"])
    assert request["parameters"]["lyrics"] == LYRICS
    assert request["parameters"]["audio_duration"] == 10
    assert library(tmp_path)[0]["editCount"] == 1


def test_repaint_inspector_failure_retains_usable_edited_audio_without_retry(tmp_path):
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    store = ProjectStore.initialize(tmp_path / "song", title="편집", lyrics=LYRICS,
                                    style_prompt="Korean acoustic pop", target_duration_seconds=10)
    parent = generate_candidates(store.root, seeds=[1], client_factory=FakeAceClient, engine="ace-step")["candidateIds"][0]
    result = repaint_candidate(store.root, candidate_id=parent, start_seconds=2, end_seconds=5, seed=8,
        client_factory=FakeAceClient, quality=quality_policy(max_attempts=1), quality_backend=Reports(error=True), engine="ace-step")
    winner = store.find_by_id(store.load(), "candidates", "candidateId", result["candidateId"])
    assert FakeAceClient.submitted == [1, 8]
    assert winner["quality"]["status"] == "unknown" and winner["quality"]["retryReasons"] == []
    assert winner["quality"].get("processing") and winner["humanReview"]["status"] == "unreviewed"


@pytest.mark.parametrize("measurement,attenuated", [
    ({"status": "measured", "truePeakDbtp": 2.0}, True),
    ({"status": "unknown", "truePeakDbtp": 2.0}, False),
    ({"status": "measured", "truePeakDbtp": True}, False),
])
def test_finishing_uses_only_valid_measured_true_peak(tmp_path, measurement, attenuated):
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    store = ProjectStore.initialize(tmp_path / "song", title="측정", lyrics=LYRICS,
                                    style_prompt="Korean acoustic pop", target_duration_seconds=10)

    class Measured(Reports):
        def analyze(self, *args, **kwargs):
            report = super().analyze(*args, **kwargs)
            report["loudness"] = measurement
            return report

    result = generate(store, backend=Measured())
    winner = store.find_by_id(store.load(), "candidates", "candidateId", result["candidateIds"][0])
    processing = winner["quality"]["processing"]
    assert (processing["gain"] < 1) is attenuated
    assert processing["peakCeiling"] < .98 if attenuated else processing["peakCeiling"] == .98
