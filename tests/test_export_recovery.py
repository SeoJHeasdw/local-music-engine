"""Export interruption, atomic-save faults, and ownership-preserving rollback."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from local_music_engine import workflow
from local_music_engine.jobs import recover_project
from local_music_engine.storage import ProjectStore, sha256_file
from local_music_engine.views import library, project_status
from test_workflow import FakeAceClient, make_project


@pytest.fixture
def selected_project(tmp_path: Path):
    root = tmp_path / "song"
    make_project(root)
    FakeAceClient.fail_seeds = set()
    FakeAceClient.submitted = []
    candidate = workflow.generate_candidates(root, seeds=[1], client_factory=FakeAceClient, engine="ace-step")["candidateIds"][0]
    workflow.select_candidate(root, candidate)
    return ProjectStore(root)


@pytest.mark.parametrize("published", ["internal", "external"])
def test_interrupt_removes_only_incomplete_outputs_and_allows_same_name_retry(
    selected_project: ProjectStore, tmp_path: Path, monkeypatch, published: str,
):
    store = selected_project
    external = tmp_path / "selected.wav"
    previous = workflow.export_selected(store.root)
    original_copy = workflow._copy_atomic

    def interrupt_after_publication(source, destination, **kwargs):
        original_copy(source, destination, **kwargs)
        if (published == "internal" and destination != external) or destination == external:
            raise KeyboardInterrupt

    monkeypatch.setattr(workflow, "_copy_atomic", interrupt_after_publication)
    with pytest.raises(KeyboardInterrupt):
        workflow.export_selected(store.root, output=external)
    project = store.load()
    job = project["jobs"][-1]
    assert job["status"] == "cancelled" and job["cancelRequested"]
    assert job["resultRefs"] == []
    assert not external.exists()
    assert all(not Path(item[key]).exists() for item in job["ownedOutputs"] for key in ("path", "temporaryPath"))
    assert store.verify_artifact(project["artifacts"][0]) == (True, "ok")
    assert Path(previous["path"]).is_file()
    assert len(project["artifacts"]) == 2
    assert not store.export_active()
    monkeypatch.setattr(workflow, "_copy_atomic", original_copy)
    assert workflow.export_selected(store.root, output=external)["externalPath"] == str(external)


@pytest.mark.parametrize("committed", [False, True])
def test_final_save_failure_preserves_original_error_and_respects_canonical_status(
    selected_project: ProjectStore, tmp_path: Path, monkeypatch, committed: bool,
):
    store = selected_project
    external = tmp_path / "selected.wav"
    original_save = ProjectStore.save
    failed = False

    def save_fault(self, project):
        nonlocal failed
        final = project["jobs"][-1]
        if not failed and final["kind"] == "export" and final["status"] == "succeeded":
            failed = True
            if committed:
                original_save(self, project)
            raise OSError("export manifest write failed")
        return original_save(self, project)

    monkeypatch.setattr(ProjectStore, "save", save_fault)
    with pytest.raises(OSError, match="export manifest write failed"):
        workflow.export_selected(store.root, output=external)
    project = store.load()
    job = project["jobs"][-1]
    assert job["status"] == ("succeeded" if committed else "failed")
    assert len(project["artifacts"]) == (2 if committed else 1)
    assert external.exists() == committed
    assert all(not Path(item["temporaryPath"]).exists() for item in job["ownedOutputs"])
    if committed:
        assert store.verify_artifact(project["artifacts"][-1]) == (True, "ok")
        assert job["resultRefs"] == [project["artifacts"][-1]["artifactId"]]
    else:
        assert job["resultRefs"] == []
        assert "export manifest write failed" in job["error"]
        assert all(not Path(item["path"]).exists() for item in job["ownedOutputs"])
        assert workflow.export_selected(store.root, output=external)["externalPath"] == str(external)


@pytest.mark.parametrize("replacement", ["new-inode", "in-place"])
def test_failure_cleanup_preserves_user_changed_output(selected_project: ProjectStore, tmp_path: Path, monkeypatch, replacement: str):
    store = selected_project
    external = tmp_path / "selected.wav"
    original_copy = workflow._copy_atomic

    def replace_after_publication(source, destination, **kwargs):
        original_copy(source, destination, **kwargs)
        if destination == external:
            if replacement == "new-inode":
                destination.unlink()
            destination.write_bytes(b"user replacement")
            raise KeyboardInterrupt

    monkeypatch.setattr(workflow, "_copy_atomic", replace_after_publication)
    with pytest.raises(KeyboardInterrupt):
        workflow.export_selected(store.root, output=external)
    assert external.read_bytes() == b"user replacement"
    job = store.load()["jobs"][-1]
    assert job["status"] == "cancelled"
    assert job["outputCleanupErrors"] and "Preserved" in job["outputCleanupErrors"][0]
    assert all(not Path(item["temporaryPath"]).exists() for item in job["ownedOutputs"])
    assert not Path(job["ownedOutputs"][0]["path"]).exists()


def test_listener_and_input_changes_during_export_are_durable(selected_project: ProjectStore, monkeypatch):
    store = selected_project
    candidate = store.load()["selectedCandidateId"]
    original_copy = workflow._copy_atomic

    def edit_during_export(source, destination, **kwargs):
        original_copy(source, destination, **kwargs)
        subprocess.run([
            sys.executable, "-m", "local_music_engine", "review", str(store.root), candidate,
            "--status", "approved", "--rating", "4", "--note", "내보내는 동안 들었어요",
        ], check=True, timeout=5, capture_output=True)
        workflow.revise_inputs(store.root, title="내보내는 동안 저장한 제목")

    monkeypatch.setattr(workflow, "_copy_atomic", edit_during_export)
    workflow.export_selected(store.root)
    project = store.load()
    assert project["title"] == "내보내는 동안 저장한 제목"
    assert project["candidates"][0]["humanReview"]["status"] == "approved"
    assert project["jobs"][-1]["status"] == "succeeded"


def test_export_sigkill_recovers_published_output_without_touching_history(selected_project: ProjectStore, tmp_path: Path):
    store = selected_project
    previous = workflow.export_selected(store.root)
    external = tmp_path / "selected.wav"
    marker = tmp_path / "published"
    script = '''
import signal, sys
from pathlib import Path
from local_music_engine import workflow
original = workflow._copy_atomic
def wait_after_publication(source, destination, **kwargs):
    original(source, destination, **kwargs)
    if destination == Path(sys.argv[2]):
        Path(sys.argv[3]).touch()
        signal.pause()
workflow._copy_atomic = wait_after_publication
workflow.export_selected(sys.argv[1], output=sys.argv[2])
'''
    child = subprocess.Popen([sys.executable, "-c", script, str(store.root), str(external), str(marker)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(0.02)
        assert marker.exists()
        assert store.export_active() and not store.generation_active()
        assert project_status(store, store.load())["jobs"][-1]["status"] == "running"
        assert library(None, [store.root])[0]["runningJobs"] == 1
        assert recover_project(store.root)["interruptedJobIds"] == []
        with pytest.raises(RuntimeError, match="export is already running"):
            workflow.export_selected(store.root, output=external)
        child.kill()
        child.wait(timeout=5)
        assert not store.export_active()
        assert project_status(store, store.load())["jobs"][-1]["status"] == "interrupted"
        assert store.load()["jobs"][-1]["status"] == "running"
        assert library(None, [store.root])[0]["interruptedJobs"] == 1
        recovered = recover_project(store.root)
        assert recovered["interruptedJobIds"] == [store.load()["jobs"][-1]["jobId"]]
        job = store.load()["jobs"][-1]
        assert job["status"] == "interrupted" and not external.exists()
        assert all(not Path(item[key]).exists() for item in job["ownedOutputs"] for key in ("path", "temporaryPath"))
        assert Path(previous["path"]).is_file()
        assert len(store.load()["artifacts"]) == 2
        workflow.export_selected(store.root, output=external)
        assert external.is_file()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_next_export_recovers_dead_export_before_reusing_filename(selected_project: ProjectStore, tmp_path: Path):
    store = selected_project
    external = tmp_path / "selected.wav"
    temporary = tmp_path / ".selected.wav.partial"
    temporary.write_bytes(b"unfinished")
    external.hardlink_to(temporary)
    identity = temporary.stat()
    with store.transaction() as project:
        job = store.append_job(project, kind="export", parameters={"candidateId": project["selectedCandidateId"]})
        store.transition_job(job, "running")
        job["ownedOutputs"] = [{"path": str(external), "temporaryPath": str(temporary),
                                "device": identity.st_dev, "inode": identity.st_ino,
                                "expectedBytes": identity.st_size, "expectedSha256": sha256_file(temporary)}]
    workflow.export_selected(store.root, output=external)
    assert external.is_file() and not temporary.exists()
    assert store.load()["jobs"][-2]["status"] == "interrupted"


@pytest.mark.parametrize("change", ["in-place", "anchor-removed"])
def test_recovery_keeps_user_modified_or_unanchored_output(selected_project: ProjectStore, tmp_path: Path, change: str):
    store = selected_project
    previous = workflow.export_selected(store.root)
    external = tmp_path / "selected.wav"
    temporary = tmp_path / ".selected.wav.partial"
    temporary.write_bytes(Path(previous["path"]).read_bytes())
    external.hardlink_to(temporary)
    identity = temporary.stat()
    with store.transaction() as project:
        job = store.append_job(project, kind="export", parameters={"candidateId": project["selectedCandidateId"]})
        store.transition_job(job, "running")
        job["ownedOutputs"] = [{"path": str(external), "temporaryPath": str(temporary),
                                "device": identity.st_dev, "inode": identity.st_ino,
                                "expectedBytes": identity.st_size, "expectedSha256": sha256_file(temporary)}]
    if change == "in-place":
        edited = bytearray(external.read_bytes())
        edited[-1] ^= 1
        external.write_bytes(edited)
    else:
        temporary.unlink()
    original = external.read_bytes()
    recover_project(store.root)
    assert external.read_bytes() == original
    assert Path(previous["path"]).is_file()
    job = store.load()["jobs"][-1]
    assert job["status"] == "interrupted"
    assert job["outputCleanupErrors"] and "Preserved" in job["outputCleanupErrors"][0]
    view = project_status(store, store.load())["jobs"][-1]
    assert view["outputCleanupErrors"] == job["outputCleanupErrors"]
    assert "some files were preserved" in view["error"]
    assert not temporary.exists()


def test_legacy_export_recovery_reports_missing_ownership(selected_project: ProjectStore):
    store = selected_project
    with store.transaction() as project:
        job = store.append_job(project, kind="export", parameters={"candidateId": project["selectedCandidateId"]})
        store.transition_job(job, "running")
    recover_project(store.root)
    view = project_status(store, store.load())["jobs"][-1]
    assert view["status"] == "interrupted"
    assert view["outputCleanupErrors"] == []
    assert "no output ownership was recorded" in view["error"]
    assert "cleaned up" not in view["error"]
