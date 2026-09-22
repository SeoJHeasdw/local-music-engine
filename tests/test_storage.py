from pathlib import Path

import pytest

from local_music_engine.storage import ProjectStore


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
