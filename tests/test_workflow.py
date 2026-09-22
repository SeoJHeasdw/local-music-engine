import math
import struct
import wave
from pathlib import Path
from typing import Any

import pytest

from local_music_engine.storage import ProjectStore
from local_music_engine.views import library, project_status
from local_music_engine.workflow import (
    export_selected,
    generate_candidates,
    repaint_candidate,
    resume_latest_batch,
    review_candidate,
    revise_inputs,
    select_candidate,
    undo_selection,
)


class FakeAceClient:
    fail_seeds: set[int] = set()
    submitted: list[int] = []

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.seed = 0

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "test",
            "loaded_model": "acestep-v15-turbo",
            "loaded_lm_model": "acestep-5Hz-lm-0.6B",
        }

    def submit(self, request: dict[str, Any], source_audio: Path | None = None) -> str:
        self.seed = int(request["seed"])
        self.submitted.append(self.seed)
        return f"task-{self.seed}"

    def wait(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        callback = kwargs.get("on_progress")
        if callback:
            callback({"status": 0, "progress": 0.5, "stage": "generating"})
        if self.seed in self.fail_seeds:
            raise RuntimeError(f"seed {self.seed} failed")
        return {
            "file": "/v1/audio?path=%2Ffake.wav",
            "generation_info": "fake",
            "seed_value": str(self.seed),
            "lm_model": "fake-lm",
            "dit_model": "fake-dit",
        }

    def download(self, file_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 8000
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            frames = [
                struct.pack(
                    "<h",
                    int(10000 * math.sin(2 * math.pi * (220 + self.seed) * i / sample_rate)),
                )
                for i in range(10 * sample_rate)
            ]
            output.writeframes(b"".join(frames))


def make_project(path: Path) -> None:
    ProjectStore.initialize(
        path,
        title="곡",
        lyrics="[Verse]\n테스트 가사",
        style_prompt="Korean pop",
        target_duration_seconds=10,
    )


def test_partial_failure_and_explicit_resume(tmp_path: Path) -> None:
    root = tmp_path / "song"
    make_project(root)
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = {2}
    first = generate_candidates(
        root, seeds=[1, 2], client_factory=FakeAceClient, poll_seconds=0.01
    )
    assert first["status"] == "partial"
    assert len(first["candidateIds"]) == 1

    FakeAceClient.fail_seeds = set()
    resumed = resume_latest_batch(
        root, client_factory=FakeAceClient, poll_seconds=0.01
    )
    assert resumed["status"] == "succeeded"
    assert resumed["reusedCandidateIds"] == first["candidateIds"]
    assert FakeAceClient.submitted == [1, 2, 2]
    assert len(ProjectStore(root).load()["candidates"]) == 2


def test_new_batch_does_not_implicitly_reuse(tmp_path: Path) -> None:
    root = tmp_path / "song"
    make_project(root)
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    generate_candidates(root, seeds=[7], client_factory=FakeAceClient)
    generate_candidates(root, seeds=[7], client_factory=FakeAceClient)
    assert FakeAceClient.submitted == [7, 7]
    assert len(ProjectStore(root).load()["candidates"]) == 2


def test_review_select_undo_and_export_preserve_source(tmp_path: Path) -> None:
    root = tmp_path / "song"
    make_project(root)
    FakeAceClient.fail_seeds = set()
    result = generate_candidates(root, seeds=[9], client_factory=FakeAceClient)
    candidate_id = result["candidateIds"][0]
    review = review_candidate(
        root, candidate_id, status="approved", rating=5, note="청취 완료"
    )
    assert review["status"] == "approved"
    select_candidate(root, candidate_id)
    external = tmp_path / "chosen.wav"
    exported = export_selected(root, output=external)
    assert external.is_file()
    assert exported["sha256"]

    store = ProjectStore(root)
    project = store.load()
    candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    source = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
    assert store.verify_artifact(source) == (True, "ok")

    undo_selection(root)
    assert ProjectStore(root).load()["selectedCandidateId"] is None


def test_feedback_regeneration_revises_inputs_and_links_feedback(tmp_path: Path) -> None:
    root = tmp_path / "song"
    make_project(root)
    FakeAceClient.fail_seeds = set()
    first = generate_candidates(root, seeds=[1], client_factory=FakeAceClient)
    source_id = first["candidateIds"][0]
    feedback = {"text": "드럼이 너무 세요", "candidateId": source_id, "plan": {"action": "regenerate"}}
    second = generate_candidates(
        root,
        seeds=[2],
        style_prompt="Korean pop, light drums",
        feedback=feedback,
        client_factory=FakeAceClient,
    )

    store = ProjectStore(root)
    project = store.load()
    assert project["inputs"]["stylePrompt"] == "Korean pop, light drums"
    change = [item for item in project["revisions"] if item["kind"] == "inputs-change"][-1]
    assert change["before"] == {"stylePrompt": "Korean pop"}
    record = project["feedback"][0]
    assert record["text"] == "드럼이 너무 세요"
    assert record["jobId"] == second["batchJobId"]

    status = project_status(store, project)
    rows = {row["candidateId"]: row for row in status["candidates"]}
    assert rows[source_id]["stylePrompt"] == "Korean pop"
    assert rows[second["candidateIds"][0]]["stylePrompt"] == "Korean pop, light drums"
    assert rows[second["candidateIds"][0]]["feedbackId"] == record["feedbackId"]


def test_repaint_uses_caption_and_strength_not_free_instruction(tmp_path: Path) -> None:
    root = tmp_path / "song"
    make_project(root)
    FakeAceClient.fail_seeds = set()
    parent = generate_candidates(root, seeds=[3], client_factory=FakeAceClient)["candidateIds"][0]
    select_candidate(root, parent)
    result = repaint_candidate(
        root,
        start_seconds=2,
        end_seconds=4,
        seed=4,
        style_prompt="Korean pop, clear Korean diction",
        strength="light",
        feedback={"text": "발음", "candidateId": parent, "range": {"startSeconds": 2, "endSeconds": 4}},
        client_factory=FakeAceClient,
    )
    project = ProjectStore(root).load()
    request = project["requests"][-1]["parameters"]
    assert request["prompt"] == "Korean pop, clear Korean diction"
    assert request["repaint_strength"] == 0.25
    assert "instruction" not in request
    assert project["inputs"]["stylePrompt"] == "Korean pop"
    assert project["selectedCandidateId"] == parent
    assert project["feedback"][0]["jobId"] == result["jobId"]


def test_cancelled_batch_does_not_leave_running_jobs(tmp_path: Path) -> None:
    root = tmp_path / "song"
    make_project(root)

    class InterruptingClient(FakeAceClient):
        def wait(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        generate_candidates(root, seeds=[5, 6], client_factory=InterruptingClient)
    jobs = ProjectStore(root).load()["jobs"]
    assert {job["status"] for job in jobs} == {"cancelled"}


def test_revise_inputs_records_history_and_rejects_empty_lyrics(tmp_path: Path) -> None:
    root = tmp_path / "song"
    make_project(root)
    revision = revise_inputs(root, title="새 제목", duration_seconds=30, bpm=92, reason="manual")
    assert revision["after"] == {"title": "새 제목", "targetDurationSeconds": 30.0, "bpm": 92}
    assert revise_inputs(root, title="새 제목") is None
    with pytest.raises(ValueError, match="Instrumental"):
        revise_inputs(root, lyrics="  ")
    assert revise_inputs(root, bpm=0)["after"] == {"bpm": None}


def test_library_summarizes_without_hashing(tmp_path: Path) -> None:
    make_project(tmp_path / "a")
    (tmp_path / "not-a-song").mkdir()
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "project.json").write_text("{", encoding="utf-8")
    rows = library(tmp_path)
    titles = sorted(row["title"] for row in rows)
    assert titles == ["broken", "곡"]
    assert next(row for row in rows if row["title"] == "broken")["error"]
