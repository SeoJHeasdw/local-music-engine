import fcntl
import hashlib
import json
import threading
from pathlib import Path

import pytest

from local_music_engine import storage
from local_music_engine.cli import build_parser, run
from local_music_engine.execution import execute_candidate
from local_music_engine.storage import ProjectStore
from local_music_engine.workflow import review_candidate, revise_inputs, select_candidate, undo_selection


def make_store(path: Path) -> ProjectStore:
    return ProjectStore.initialize(path, title="test", lyrics="lyrics", style_prompt="style", target_duration_seconds=10)


def test_project_round_trip_and_job_transitions(tmp_path: Path) -> None:
    store = ProjectStore.initialize(
        tmp_path / "song",
        title="테스트 곡",
        lyrics="첫 줄\r\n둘째 줄",
        style_prompt="Korean indie pop",
        target_duration_seconds=30,
    )
    project = store.load()
    assert project["schemaVersion"] == 1
    assert project["inputs"]["lyricsOriginal"] == "첫 줄\r\n둘째 줄"
    assert project["inputs"]["lyricsNormalized"] == "첫 줄\n둘째 줄"

    job = store.append_job(project, kind="test", parameters={})
    store.transition_job(job, "running")
    store.transition_job(job, "succeeded")
    with pytest.raises(ValueError, match="invalid job transition"):
        store.transition_job(job, "running")
    store.save(project)
    assert not list(store.root.glob(".project.json.*.tmp"))


def test_artifact_paths_cannot_escape_project(tmp_path: Path) -> None:
    store = ProjectStore.initialize(
        tmp_path / "song",
        title="test",
        lyrics="lyrics",
        style_prompt="style",
        target_duration_seconds=10,
    )
    with pytest.raises(ValueError, match="inside project"):
        store.relative_path(tmp_path / "outside.wav")


def test_manifest_is_compact_json_and_save_leaves_the_callers_record_alone(tmp_path: Path) -> None:
    store = make_store(tmp_path / "song")
    project = store.load()
    project.update(title="새 제목", updatedAt="before")
    store.save(project)
    assert project["updatedAt"] == "before"
    text = store.manifest_path.read_text(encoding="utf-8")
    assert "\n" not in text.rstrip("\n") and ": " not in text
    saved = json.loads(text)
    assert saved["title"] == "새 제목" and saved["updatedAt"].endswith("Z")
    assert store.load() == saved


@pytest.mark.parametrize("change", [
    lambda path: select_candidate(path, "candidate_x"),
    lambda path: undo_selection(path),
    lambda path: review_candidate(path, "candidate_x", status="listened"),
    lambda path: revise_inputs(path, title="t"),
])
def test_a_mistyped_project_path_leaves_nothing_behind(tmp_path: Path, change) -> None:
    missing = tmp_path / "songs" / "typo"
    with pytest.raises(FileNotFoundError, match="project not found"):
        change(missing)
    assert not (tmp_path / "songs").exists()

    plain = tmp_path / "folder"
    plain.mkdir()
    with pytest.raises(FileNotFoundError, match="project not found"):
        change(plain)
    assert list(plain.iterdir()) == []


def test_run_locks_wait_out_status_probes_but_refuse_a_real_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path / "song")
    locks = [(".generation.lock", lambda: store.generation_lock(lane=False), store.generation_active),
             (".export.lock", store.export_lock, store.export_active)]
    for _, take, probe in locks:
        stop = threading.Event()

        def poll() -> None:  # what library/status do while the app shows the song
            while not stop.is_set():
                probe()

        poller = threading.Thread(target=poll)
        poller.start()
        try:
            for _ in range(300):
                with take():
                    pass
        finally:
            stop.set()
            poller.join()

    monkeypatch.setattr(storage, "LOCK_PROBE_WAIT_SECONDS", 0.2)
    for name, take, probe in locks:
        with (store.root / name).open("a+b") as owner:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
            assert probe()
            with pytest.raises(RuntimeError, match="already running"):
                with take():
                    pass


def test_progress_is_saved_only_when_the_app_would_show_a_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path / "song")
    with store.transaction() as project:
        job = store.append_job(project, kind="candidate", parameters={})
        store.transition_job(job, "running", stage="submitting")
    writes = []
    write = storage.atomic_write_json
    monkeypatch.setattr(storage, "atomic_write_json", lambda path, value, **options: (writes.append(path), write(path, value, **options)))
    polls = [("queued", 0.0)] * 5 + [("generating", 0.2), ("generating", 0.204), ("generating", 0.21),
                                     ("generating", 0.0), ("generating", 0.5), ("decoding", 0.0)]
    seen = []

    class Client:
        def submit(self, payload, source_audio=None):
            return "task-1"

        def wait(self, task_id, *, on_progress, **options):
            for stage, value in polls:
                before = len(writes)
                on_progress({"stage": stage, "progress": value})
                saved = store.find_by_id(store.load(), "jobs", "jobId", job["jobId"])
                seen.append((len(writes) - before, saved["stage"], saved["progress"]))
            raise RuntimeError("stop after the polls")

    request = {"requestId": "request-1", "parameters": {"seed": 1, "audio_duration": 10}}
    with pytest.raises(RuntimeError, match="stop after the polls"):
        execute_candidate(store, Client(), request=request, job_id=job["jobId"], api_payload={}, poll_seconds=1, timeout_seconds=5)
    assert seen == [
        (1, "queued", 0.0), (0, "queued", 0.0), (0, "queued", 0.0), (0, "queued", 0.0), (0, "queued", 0.0),
        (1, "generating", 0.2), (0, "generating", 0.2), (1, "generating", 0.21),
        (0, "generating", 0.21), (1, "generating", 0.5), (1, "decoding", 0.5),  # never moves backwards
    ]


def test_inspect_does_not_count_a_replaced_listening_copy_as_damage(tmp_path: Path) -> None:
    store = make_store(tmp_path / "song")
    audio = store.root / "artifacts" / "listening" / "new.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"new copy")

    def record(artifact_id: str, path: str, data: bytes, **extra) -> dict:
        return {"artifactId": artifact_id, "kind": "listening-audio", "path": path, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), **extra}

    with store.transaction() as project:
        project["artifacts"] += [
            record("artifact_old", "artifacts/listening/old.wav", b"old copy",
                   retired={"supersededBy": "artifact_new", "at": "2026-10-14T00:00:00.000Z"}),
            record("artifact_new", "artifacts/listening/new.wav", b"new copy"),
        ]
    inspect = build_parser().parse_args(["inspect", str(store.root)])
    report = run(inspect)
    assert report["restorable"] and report["invalidArtifacts"] == []
    assert report["retiredArtifacts"] == [{"artifactId": "artifact_old", "supersededBy": "artifact_new"}]

    audio.unlink()
    report = run(inspect)
    assert not report["restorable"] and [item["artifactId"] for item in report["invalidArtifacts"]] == ["artifact_new"]
