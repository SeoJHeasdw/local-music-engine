import math
import struct
import wave
from pathlib import Path
from typing import Any

from local_music_engine.storage import ProjectStore
from local_music_engine.workflow import (
    export_selected,
    generate_candidates,
    resume_latest_batch,
    review_candidate,
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
