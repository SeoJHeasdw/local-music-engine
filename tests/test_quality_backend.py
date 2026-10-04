"""Subprocess ownership contracts, without loading models or starting inference."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from local_music_engine import quality_backend
from local_music_engine.quality_backend import AudioOnlyBackend, LocalQualityBackend


def command(script: str) -> list[str]:
    return [sys.executable, "-u", "-c", script]


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An orphan can briefly remain a zombie until the system reaps it. A zombie
    # has exited and owns neither model memory nor a running inspection task.
    status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False).stdout.strip()
    return bool(status) and not status.startswith("Z")


def assert_stopped(pid: int) -> None:
    deadline = time.monotonic() + 2
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not process_alive(pid), f"owned analysis process {pid} is still running"


def cleanup_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.fixture(autouse=True)
def short_termination_grace(monkeypatch):
    monkeypatch.setattr(quality_backend, "TERMINATE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(quality_backend, "KILL_DRAIN_SECONDS", 0.5)


def test_runner_returns_json_and_passes_parent_identity_and_private_analysis_environment() -> None:
    report = LocalQualityBackend._run(command("""
import json, os
print(json.dumps({key: os.environ.get(key) for key in (
    'MUSIC_ENGINE_QUALITY_PARENT_PID', 'HF_HUB_DISABLE_TELEMETRY', 'DO_NOT_TRACK', 'PYTHONUNBUFFERED'
)}))
"""), timeout=2)
    assert report["MUSIC_ENGINE_QUALITY_PARENT_PID"] == str(os.getpid())
    assert report["HF_HUB_DISABLE_TELEMETRY"] == report["DO_NOT_TRACK"] == report["PYTHONUNBUFFERED"] == "1"


def test_timeout_releases_active_analysis_process(tmp_path: Path) -> None:
    pid_path = tmp_path / "analysis.pid"
    script = f"import os,time,pathlib; pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(30)"
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="inspection timed out"):
        LocalQualityBackend._run(command(script), timeout=0.1)
    assert time.monotonic() - started < 2
    assert_stopped(int(pid_path.read_text()))


def test_user_cancellation_propagates_and_releases_analysis_process(tmp_path: Path) -> None:
    pid_path = tmp_path / "cancelled.pid"
    script = f"""
import os,sys,time,pathlib
pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))
print('Inspecting vocals', file=sys.stderr, flush=True)
time.sleep(30)
"""
    messages = []

    def cancel(message):
        messages.append(message)
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        LocalQualityBackend._run(command(script), timeout=2, progress=cancel)
    assert messages == ["Inspecting vocals"]
    assert_stopped(int(pid_path.read_text()))


@pytest.mark.parametrize("leader_exits", [False, True])
def test_termination_escalates_for_descendant_holding_pipes_after_leader_exits(tmp_path: Path, leader_exits: bool) -> None:
    pid_path = tmp_path / "tree.json"
    child_script = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    script = f"""
import json,os,pathlib,subprocess,sys,time
child = subprocess.Popen([sys.executable, '-u', '-c', {child_script!r}])
time.sleep(0.1)
pathlib.Path({str(pid_path)!r}).write_text(json.dumps({{'leader':os.getpid(),'descendant':child.pid}}))
{'sys.exit(0)' if leader_exits else 'time.sleep(30)'}
"""
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            LocalQualityBackend._run(command(script), timeout=0.15)
        assert time.monotonic() - started < 2
        pids = json.loads(pid_path.read_text())
        assert_stopped(pids["leader"])
        assert_stopped(pids["descendant"])
    finally:
        if pid_path.exists():
            cleanup_group(json.loads(pid_path.read_text())["leader"])


def test_success_also_releases_owned_descendants_with_redirected_output(tmp_path: Path) -> None:
    pid_path = tmp_path / "redirected.json"
    child_script = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    script = f"""
import json,os,pathlib,subprocess,sys,time
child = subprocess.Popen([sys.executable, '-u', '-c', {child_script!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.1)
pids = {{'leader':os.getpid(),'descendant':child.pid}}
pathlib.Path({str(pid_path)!r}).write_text(json.dumps(pids))
print(json.dumps({{'result':'complete'}}))
"""
    try:
        report = LocalQualityBackend._run(command(script), timeout=2)
        assert report == {"result": "complete"}
        assert_stopped(json.loads(pid_path.read_text())["descendant"])
    finally:
        if pid_path.exists():
            cleanup_group(json.loads(pid_path.read_text())["leader"])


def test_nonzero_exit_reports_diagnostic_and_does_not_fake_json_success() -> None:
    with pytest.raises(RuntimeError, match="model could not be loaded"):
        LocalQualityBackend._run(command("import sys; print('model could not be loaded',file=sys.stderr); sys.exit(2)"), timeout=2)


def test_bad_json_and_oversize_reports_are_rejected() -> None:
    with pytest.raises(json.JSONDecodeError):
        LocalQualityBackend._run(command("print('not a report')"), timeout=2)
    with pytest.raises(ValueError, match="report is too large"):
        LocalQualityBackend._run(command("print('x'*4000001)"), timeout=2)
    assert LocalQualityBackend._run(command("print('setup progress')"), timeout=2, json_output=False) is None


def test_offline_missing_setup_returns_unknown_with_measurements_and_never_downloads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(quality_backend, "RUNTIME", tmp_path / "absent-runtime")
    backend = LocalQualityBackend(auto_setup=False)
    monkeypatch.setattr(backend, "_run", lambda *args, **kwargs: pytest.fail("offline analysis cannot bootstrap or load models"))
    measured = {"status": "measured", "truePeakDbtp": -2}
    monkeypatch.setattr(backend, "measure_loudness", lambda audio: measured)
    report = backend.analyze(tmp_path / "audio.wav")
    assert report["available"] is False and report["reliable"] is False
    assert report["text"] == "" and report["segments"] == []
    assert report["loudness"] == measured
    assert "not installed" in report["error"]


def test_analysis_timeout_is_unknown_and_cancellation_still_propagates(tmp_path: Path, monkeypatch) -> None:
    backend = LocalQualityBackend(auto_setup=False)
    monkeypatch.setattr(backend, "prepare", lambda progress=None: True)
    monkeypatch.setattr(backend, "measure_loudness", lambda audio: {"status": "unknown"})

    def timed_out(*args, **kwargs):
        raise TimeoutError("inspection timed out")

    monkeypatch.setattr(backend, "_run", timed_out)
    report = backend.analyze(tmp_path / "audio.wav")
    assert report["available"] is False and report["reliable"] is False
    assert "TimeoutError" in report["error"]

    def cancelled(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(backend, "_run", cancelled)
    with pytest.raises(KeyboardInterrupt):
        backend.analyze(tmp_path / "audio.wav")


def test_invalid_worker_result_or_loudness_cannot_become_a_successful_measurement(tmp_path: Path, monkeypatch) -> None:
    backend = LocalQualityBackend(auto_setup=False)
    monkeypatch.setattr(backend, "prepare", lambda progress=None: True)
    monkeypatch.setattr(LocalQualityBackend, "_run", lambda *args, **kwargs: [])
    report = backend.analyze(tmp_path / "audio.wav")
    assert report["available"] is False and report["reliable"] is False
    assert report["loudness"]["status"] == "unknown"


def test_audio_only_backend_does_not_report_a_lyric_match(tmp_path: Path, monkeypatch) -> None:
    backend = AudioOnlyBackend()
    monkeypatch.setattr(backend, "measure_loudness", lambda audio: {"status": "measured", "truePeakDbtp": -3})
    report = backend.analyze(tmp_path / "audio.wav")
    assert report["available"] is False and report["reliable"] is False
    assert report["text"] == "" and report["segments"] == []
    assert report["loudness"]["truePeakDbtp"] == -3
