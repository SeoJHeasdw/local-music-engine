"""Process death, concurrent listeners, and immutable resume provenance."""

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from local_music_engine.jobs import recover_project
from local_music_engine.storage import ProjectStore
from local_music_engine.views import library, project_status
from local_music_engine.workflow import (
    export_selected, generate_candidates, repaint_candidate, resume_latest_batch,
    revise_inputs, select_candidate, undo_selection,
)
from test_workflow import FakeAceClient, make_project


@pytest.fixture(autouse=True)
def reset_client():
    FakeAceClient.fail_seeds = set()
    FakeAceClient.submitted = []


def test_resume_freezes_all_original_inputs_and_does_not_revert_new_defaults(tmp_path: Path):
    make_project(tmp_path)
    FakeAceClient.fail_seeds = {2}
    first = generate_candidates(tmp_path, seeds=[1, 2], bpm=88, client_factory=FakeAceClient)
    revise_inputs(tmp_path, lyrics="새 가사", style_prompt="jazz", duration_seconds=120, bpm=140)
    FakeAceClient.fail_seeds.clear()
    result = resume_latest_batch(tmp_path, client_factory=FakeAceClient)
    project = ProjectStore(tmp_path).load()
    assert result["reusedCandidateIds"] == first["candidateIds"]
    assert len(result["newCandidateIds"]) == 1
    assert project["requests"][-1]["parameters"]["lyrics"] == "[Verse]\n테스트 가사"
    assert project["requests"][-1]["parameters"]["prompt"] == "Korean pop"
    assert project["requests"][-1]["parameters"]["bpm"] == 88
    assert project["requests"][-1]["parameters"]["audio_duration"] == 10
    assert project["inputs"]["stylePrompt"] == "jazz"
    assert project["inputs"]["targetDurationSeconds"] == 120
    batch = next(job for job in project["jobs"] if job["jobId"] == result["batchJobId"])
    assert batch["parameters"]["resumeOfJobId"] == first["batchJobId"]


def test_resume_cannot_borrow_same_seed_from_unrelated_batch(tmp_path: Path):
    make_project(tmp_path)
    generate_candidates(tmp_path, seeds=[1, 2], client_factory=FakeAceClient)
    FakeAceClient.fail_seeds = {2}
    failed = generate_candidates(tmp_path, seeds=[2], client_factory=FakeAceClient)
    FakeAceClient.fail_seeds.clear()
    unrelated = generate_candidates(tmp_path, seeds=[2], client_factory=FakeAceClient)
    resumed = resume_latest_batch(tmp_path, job_id=failed["batchJobId"], client_factory=FakeAceClient)
    assert resumed["reusedCandidateIds"] == []
    assert resumed["candidateIds"] != unrelated["candidateIds"]
    assert FakeAceClient.submitted == [1, 2, 2, 2, 2]


@pytest.mark.parametrize("damage", ["missing", "size", "hash"])
def test_resume_regenerates_only_damaged_outputs(tmp_path: Path, damage: str):
    make_project(tmp_path)
    first = generate_candidates(tmp_path, seeds=[1, 2], client_factory=FakeAceClient)
    store = ProjectStore(tmp_path)
    artifact = store.load()["artifacts"][0]
    audio = store.resolve_artifact(artifact)
    if damage == "missing":
        audio.unlink()
    elif damage == "size":
        audio.write_bytes(b"broken")
    else:
        data = bytearray(audio.read_bytes())
        data[-1] ^= 1
        audio.write_bytes(data)
    result = resume_latest_batch(tmp_path, client_factory=FakeAceClient)
    assert result["reusedCandidateIds"] == first["candidateIds"][1:]
    assert FakeAceClient.submitted == [1, 2, 1]
    assert len(store.load()["candidates"]) == 3


def test_complete_resume_needs_no_engine_and_keeps_human_review(tmp_path: Path):
    make_project(tmp_path)
    first = generate_candidates(tmp_path, seeds=[1], client_factory=FakeAceClient)

    class Offline(FakeAceClient):
        def health(self):
            raise RuntimeError("offline")

    resumed = resume_latest_batch(tmp_path, client_factory=Offline)
    again = resume_latest_batch(tmp_path, client_factory=Offline)
    assert resumed["newCandidateIds"] == again["newCandidateIds"] == []
    assert resumed["candidateIds"] == again["candidateIds"] == first["candidateIds"]
    assert len(ProjectStore(tmp_path).load()["candidates"]) == 1


def test_legacy_batch_uses_saved_child_request(tmp_path: Path):
    make_project(tmp_path)
    FakeAceClient.fail_seeds = {2}
    generate_candidates(tmp_path, seeds=[1, 2], client_factory=FakeAceClient)
    store = ProjectStore(tmp_path)
    with store.transaction() as project:
        project["jobs"][0]["parameters"].pop("frozenPayloads")
        project["inputs"]["stylePrompt"] = "changed"
    FakeAceClient.fail_seeds.clear()
    resumed = resume_latest_batch(tmp_path, client_factory=FakeAceClient)
    assert len(resumed["reusedCandidateIds"]) == 1
    assert store.load()["requests"][-1]["parameters"]["prompt"] == "Korean pop"


def test_unknown_legacy_inputs_fail_without_mutating_history(tmp_path: Path):
    make_project(tmp_path)
    store = ProjectStore(tmp_path)
    with store.transaction() as project:
        store.append_job(project, kind="candidate-batch", parameters={"seeds": [1]})
    before = store.manifest_path.read_bytes()
    with pytest.raises(ValueError, match="original batch inputs"):
        resume_latest_batch(tmp_path, client_factory=FakeAceClient)
    assert store.manifest_path.read_bytes() == before
    view = project_status(store, store.load())["jobs"][0]
    assert not view["canResume"] and view["resumeBlockedReason"]


def test_review_and_input_changes_during_inference_are_durable(tmp_path: Path):
    make_project(tmp_path)
    first = generate_candidates(tmp_path, seeds=[1], client_factory=FakeAceClient)
    candidate_id = first["candidateIds"][0]

    class ListeningClient(FakeAceClient):
        def wait(self, task_id: str, **kwargs: Any):
            # A separate process must finish while this inference still owns its
            # generation lease. A long manifest lock would time out this subprocess.
            subprocess.run([
                sys.executable, "-m", "local_music_engine", "review", str(tmp_path),
                candidate_id, "--status", "approved", "--rating", "4", "--note", "생성 중 들었어요",
            ], check=True, timeout=5, capture_output=True)
            revise_inputs(tmp_path, style_prompt="next song style", duration_seconds=45)
            return super().wait(task_id, **kwargs)

    generate_candidates(tmp_path, seeds=[2, 3], client_factory=ListeningClient)
    project = ProjectStore(tmp_path).load()
    review = project["candidates"][0]["humanReview"]
    assert review["status"] == "approved" and review["rating"] == 4
    assert len(review["notes"]) == 2
    assert project["inputs"]["stylePrompt"] == "next song style"
    assert all(request["parameters"]["prompt"] == "Korean pop" for request in project["requests"])
    assert all(request["parameters"]["audio_duration"] == 10 for request in project["requests"])


def test_sigkill_releases_lease_and_completed_work_survives(tmp_path: Path):
    make_project(tmp_path)
    marker = tmp_path / "waiting"
    script = '''
import sys, signal
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from test_workflow import FakeAceClient
from local_music_engine.workflow import generate_candidates
class BlockingClient(FakeAceClient):
    def wait(self, task_id, **kwargs):
        if self.seed == 2:
            Path(sys.argv[1], "waiting").touch()
            signal.pause()
        return super().wait(task_id, **kwargs)
generate_candidates(sys.argv[1], seeds=[1, 2, 3], client_factory=BlockingClient)
'''
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(Path(__file__).parent)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(0.02)
        assert marker.exists()
        store = ProjectStore(tmp_path)
        assert store.generation_active()
        with pytest.raises(RuntimeError, match="already running"):
            generate_candidates(tmp_path, seeds=[9], client_factory=FakeAceClient)
        with pytest.raises(RuntimeError, match="already running"):
            recover_project(tmp_path)
        before = store.load()
        assert len(before["jobs"][0]["resultRefs"]) == 1
        assert before["jobs"][-1]["remoteTaskId"] == "task-2"
        child.kill()
        child.wait(timeout=5)
        assert not store.generation_active()
        status = project_status(store, store.load())
        assert status["jobs"][0]["status"] == "interrupted"
        assert status["jobs"][0]["canResume"]
        assert store.load()["jobs"][0]["status"] == "running"  # status is read-only
        assert library(None, [tmp_path])[0]["runningJobs"] == 0
        recovered = recover_project(tmp_path)
        assert len(recovered["interruptedJobIds"]) == 2
        revise_inputs(tmp_path, style_prompt="new default", lyrics="new lyrics")
        resumed = resume_latest_batch(tmp_path, client_factory=FakeAceClient)
        assert len(resumed["reusedCandidateIds"]) == 1
        assert len(resumed["newCandidateIds"]) == 2
        assert FakeAceClient.submitted == [2, 3]
        final = store.load()
        assert not any(job["status"] in {"queued", "running"} for job in final["jobs"])
        assert final["requests"][-1]["parameters"]["lyrics"] == "[Verse]\n테스트 가사"
        assert not any(job["canResume"] for job in project_status(store, final)["jobs"])
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_repaint_uses_parent_inputs_and_actual_duration(tmp_path: Path):
    make_project(tmp_path)
    first = generate_candidates(tmp_path, seeds=[1], bpm=88, client_factory=FakeAceClient)
    revise_inputs(tmp_path, style_prompt="jazz", lyrics="other song", duration_seconds=120, bpm=180)
    repaint_candidate(tmp_path, candidate_id=first["candidateIds"][0], start_seconds=1, end_seconds=3,
                      seed=2, lyrics="수정한 가사", client_factory=FakeAceClient)
    project = ProjectStore(tmp_path).load()
    payload = project["requests"][-1]["parameters"]
    assert payload["lyrics"] == "수정한 가사"
    assert payload["prompt"] == "Korean pop"
    assert payload["audio_duration"] == 10
    assert payload["bpm"] == 88
    assert project["inputs"]["lyricsOriginal"] == "other song"
    assert project["candidates"][-1]["humanReview"]["status"] == "unreviewed"


def test_regeneration_of_old_version_starts_from_its_inputs_and_keeps_feedback_on_resume(tmp_path: Path):
    make_project(tmp_path)
    parent = generate_candidates(tmp_path, seeds=[1], bpm=88, client_factory=FakeAceClient)["candidateIds"][0]
    revise_inputs(tmp_path, style_prompt="other", lyrics="other lyrics", duration_seconds=60, bpm=150)
    FakeAceClient.fail_seeds = {2}
    generate_candidates(tmp_path, seeds=[2], source_candidate_id=parent,
                        style_prompt="Korean pop, softer drums", feedback={"text": "드럼을 줄여 주세요", "candidateId": parent},
                        client_factory=FakeAceClient)
    FakeAceClient.fail_seeds.clear()
    resumed = resume_latest_batch(tmp_path, client_factory=FakeAceClient)
    store = ProjectStore(tmp_path)
    project = store.load()
    payload = project["requests"][-1]["parameters"]
    assert payload["lyrics"] == "[Verse]\n테스트 가사"
    assert payload["bpm"] == 88 and payload["audio_duration"] == 10
    assert payload["prompt"] == "Korean pop, softer drums"
    row = next(row for row in project_status(store, project)["candidates"] if row["candidateId"] == resumed["newCandidateIds"][0])
    assert row["feedbackId"] == project["feedback"][0]["feedbackId"]


def test_cancel_preserves_completed_refs(tmp_path: Path):
    make_project(tmp_path)

    class Interrupt(FakeAceClient):
        def wait(self, task_id: str, **kwargs: Any):
            if self.seed == 2:
                raise KeyboardInterrupt
            return super().wait(task_id, **kwargs)

    with pytest.raises(KeyboardInterrupt):
        generate_candidates(tmp_path, seeds=[1, 2, 3], client_factory=Interrupt)
    project = ProjectStore(tmp_path).load()
    assert project["jobs"][0]["status"] == "cancelled"
    assert project["jobs"][0]["resultRefs"] == [project["candidates"][0]["candidateId"]]
    resumed = resume_latest_batch(tmp_path, client_factory=FakeAceClient)
    assert len(resumed["reusedCandidateIds"]) == 1


def test_export_never_overwrites_manifest_source_or_previous_file(tmp_path: Path):
    root = tmp_path / "song"
    make_project(root)
    first = generate_candidates(root, seeds=[1], client_factory=FakeAceClient)
    select_candidate(root, first["candidateIds"][0])
    store = ProjectStore(root)
    source = store.resolve_artifact(store.load()["artifacts"][0])
    original = source.read_bytes()
    for destination in [store.manifest_path, source, root / "nested" / "export.wav"]:
        with pytest.raises(ValueError, match="outside the project"):
            export_selected(root, output=destination)
    external = tmp_path / "chosen.wav"
    export_selected(root, output=external)
    with pytest.raises(FileExistsError, match="already exists"):
        export_selected(root, output=external)
    assert external.read_bytes() == original == source.read_bytes()
    assert len(store.load()["artifacts"]) == 2


def test_undo_refuses_to_select_damaged_audio(tmp_path: Path):
    make_project(tmp_path)
    result = generate_candidates(tmp_path, seeds=[1, 2], client_factory=FakeAceClient)
    for candidate in result["candidateIds"]:
        select_candidate(tmp_path, candidate)
    store = ProjectStore(tmp_path)
    store.resolve_artifact(store.load()["artifacts"][0]).unlink()
    before = store.manifest_path.read_bytes()
    with pytest.raises(ValueError, match="missing artifact"):
        undo_selection(tmp_path)
    assert store.manifest_path.read_bytes() == before


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_inputs_never_enter_manifest(tmp_path: Path, value: float):
    make_project(tmp_path)
    store = ProjectStore(tmp_path)
    before = store.manifest_path.read_bytes()
    with pytest.raises(ValueError):
        revise_inputs(tmp_path, duration_seconds=value)
    with pytest.raises(ValueError):
        repaint_candidate(tmp_path, start_seconds=0, end_seconds=value, seed=1, client_factory=FakeAceClient)
    assert store.manifest_path.read_bytes() == before
