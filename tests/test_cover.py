import math
from copy import deepcopy
from pathlib import Path

import pytest

from local_music_engine.auto_quality import prepare_payload, quality_policy
from local_music_engine.storage import ProjectStore, sha256_file
from local_music_engine.views import candidate_rows, project_status
from local_music_engine.workflow import _frozen_generation_payload, cover_candidates, generate_candidates, resume_latest_batch, revise_inputs
from test_workflow import FakeAceClient, make_project


class RecordingCoverClient(FakeAceClient):
    requests = []
    uploads = []

    def submit(self, request, source_audio=None):
        self.requests.append(deepcopy(request))
        self.uploads.append(source_audio)
        return super().submit(request, source_audio)


@pytest.fixture(autouse=True)
def clear_recordings():
    RecordingCoverClient.requests = []
    RecordingCoverClient.uploads = []
    RecordingCoverClient.submitted = []
    RecordingCoverClient.fail_seeds = set()


def source_song(root: Path):
    make_project(root)
    result = generate_candidates(root, seeds=[1], client_factory=RecordingCoverClient, engine="ace-step")
    return result["candidateIds"][0]


def test_cover_uploads_verified_source_and_keeps_project_inputs_and_original(tmp_path):
    root = tmp_path / "song"
    parent = source_song(root)
    store = ProjectStore(root)
    before = store.load()
    artifact = before["artifacts"][0]
    original = store.resolve_artifact(artifact)
    source_hash = sha256_file(original)
    result = cover_candidates(root, candidate_id=parent, seeds=[2], strength=0.72,
                              style_prompt="emotional hip hop, warm piano", lyrics="[Verse]\nTake one breath",
                              client_factory=RecordingCoverClient, engine="ace-step")
    request = RecordingCoverClient.requests[-1]
    assert request["task_type"] == "cover" and request["thinking"] is False
    assert request["use_cot_caption"] is False and request["use_cot_language"] is False
    assert request["audio_cover_strength"] == 0.72
    assert request["audio_duration"] == 10
    assert RecordingCoverClient.uploads[-1] == original
    assert "coverSource" not in request and "source_artifact_sha256" not in request
    after = store.load()
    assert after["inputs"] == before["inputs"]
    assert after["selectedCandidateId"] is None
    assert sha256_file(original) == source_hash
    candidate = next(c for c in after["candidates"] if c["candidateId"] == result["candidateIds"][0])
    assert candidate["parentCandidateId"] == parent and candidate["humanReview"]["status"] == "unreviewed"
    row = next(c for c in candidate_rows(store, after) if c["candidateId"] == candidate["candidateId"])
    assert row["coverSource"]["sha256"] == source_hash and row["coverStrength"] == 0.72
    assert "artifacts/covers/" in row["path"]
    assert project_status(store, after)["jobs"][-1]["kind"] == "cover-batch"


@pytest.mark.parametrize("strength", [True, -0.01, 1.01, math.nan, math.inf, "0.7"])
def test_invalid_cover_strength_does_not_create_a_job(tmp_path, strength):
    root = tmp_path / "song"
    parent = source_song(root)
    before = ProjectStore(root).manifest_path.read_bytes()
    with pytest.raises(ValueError, match="strength"):
        cover_candidates(root, candidate_id=parent, seeds=[2], strength=strength, client_factory=RecordingCoverClient, engine="ace-step")
    assert ProjectStore(root).manifest_path.read_bytes() == before
    assert len(RecordingCoverClient.requests) == 1


def test_corrupted_source_is_rejected_before_new_cover_records(tmp_path):
    root = tmp_path / "song"
    parent = source_song(root)
    store = ProjectStore(root)
    path = store.resolve_artifact(store.load()["artifacts"][0])
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    before = store.manifest_path.read_bytes()
    with pytest.raises(ValueError, match="SHA|sha|hash"):
        cover_candidates(root, candidate_id=parent, seeds=[2], client_factory=RecordingCoverClient, engine="ace-step")
    assert store.manifest_path.read_bytes() == before
    assert len(RecordingCoverClient.requests) == 1


def test_partial_cover_resume_uses_frozen_source_and_only_verified_completed_outputs(tmp_path):
    root = tmp_path / "song"
    parent = source_song(root)
    RecordingCoverClient.fail_seeds = {3}
    first = cover_candidates(root, candidate_id=parent, seeds=[2, 3], strength=0.6,
                             style_prompt="warm piano", client_factory=RecordingCoverClient, engine="ace-step")
    assert first["status"] == "partial"
    revise_inputs(root, style_prompt="unrelated future input", lyrics="다음 곡 가사")
    RecordingCoverClient.fail_seeds = set()
    result = resume_latest_batch(root, job_id=first["batchJobId"], client_factory=RecordingCoverClient, engine="ace-step")
    assert result["status"] == "succeeded" and result["reusedCandidateIds"] == first["candidateIds"]
    assert RecordingCoverClient.submitted == [1, 2, 3, 3]
    assert RecordingCoverClient.requests[-1]["prompt"] == "warm piano"
    assert RecordingCoverClient.requests[-1]["task_type"] == "cover"
    assert RecordingCoverClient.uploads[-1] == RecordingCoverClient.uploads[-2]
    assert ProjectStore(root).load()["inputs"]["stylePrompt"] == "unrelated future input"
    assert project_status(ProjectStore(root), ProjectStore(root).load())["jobs"][-1]["kind"] == "cover-batch"


def test_quality_preparation_never_extends_or_adds_sections_to_a_cover():
    payload = {"lyrics": "많은 가사 " * 300, "prompt": "slow ballad", "audio_duration": 10,
               "task_type": "cover", "bpm": 60, "seed": 1}
    prepared = prepare_payload(payload)
    assert prepared["lyrics"] == payload["lyrics"] and prepared["audio_duration"] == 10
    assert prepared["qualityPreparation"]["changes"] == []


def test_cover_quality_checks_once_and_records_the_reference_in_finished_version(tmp_path):
    from test_auto_quality import Reports

    root = tmp_path / "song"
    parent = source_song(root)
    result = cover_candidates(root, candidate_id=parent, seeds=[2], lyrics="[Verse]\n테스트 가사",
                              client_factory=RecordingCoverClient, quality=quality_policy(max_attempts=4),
                              quality_backend=Reports(["테스트 가사"]), engine="ace-step")
    assert result["status"] == "succeeded" and RecordingCoverClient.submitted == [1, 2]
    store = ProjectStore(root)
    project = store.load()
    cover_batch = next(j for j in project["jobs"] if j["jobId"] == result["batchJobId"])
    assert cover_batch["parameters"]["qualityPolicy"]["maxAttempts"] == 1
    assert len(cover_batch["parameters"]["qualityPlan"][0]) == 1
    winner = next(c for c in candidate_rows(store, project) if c["candidateId"] == result["recommendedCandidateId"])
    assert winner["coverStrength"] == 0.7 and winner["coverSource"]["candidateId"] == parent
    assert winner["humanReview"]["status"] == "unreviewed"


def test_cover_rechecks_frozen_source_before_each_upload(tmp_path):
    root = tmp_path / "song"
    parent = source_song(root)
    store = ProjectStore(root)
    source = store.resolve_artifact(store.load()["artifacts"][0])

    class TamperingClient(RecordingCoverClient):
        def download(self, file_path, destination):
            super().download(file_path, destination)
            data = bytearray(source.read_bytes())
            data[-1] ^= 1
            source.write_bytes(data)

    result = cover_candidates(root, candidate_id=parent, seeds=[2, 3], client_factory=TamperingClient, engine="ace-step")
    assert result["status"] == "partial"
    assert len(result["candidateIds"]) == 1
    assert RecordingCoverClient.submitted == [1, 2]
    assert "sha" in result["failures"][0]["error"].lower() or "hash" in result["failures"][0]["error"].lower()


def test_quality_length_extension_keeps_song_plan_in_sync_and_original_words(tmp_path):
    from local_music_engine.lyric_quality import _units

    lyrics = "[Verse]\n" + "긴 밤에도 작은 빛을 찾아 천천히 걸어가 " * 30
    store = ProjectStore.initialize(tmp_path / "dense", title="곡", lyrics=lyrics,
        style_prompt="piano ballad", target_duration_seconds=10,
        production_rules={"version": 1, "presetId": None, "ruleIds": ["section-development", "phrase-breathing"]})
    frozen = _frozen_generation_payload(store.load(), seed=1, model="acestep-v15-turbo", lm_model="acestep-5Hz-lm-4B")
    prepared = prepare_payload(frozen)
    assert prepared["audio_duration"] > frozen["audio_duration"]
    assert prepared["songPlan"]["arrangement"][-1]["endSeconds"] == prepared["audio_duration"]
    assert prepared["productionRules"]["songPlan"] == prepared["songPlan"]
    assert _units(prepared["lyrics"]) == _units(lyrics)
    assert prepared["qualityPreparation"]["lyricsOriginal"] == lyrics
    assert store.load()["inputs"]["lyricsOriginal"] == lyrics
