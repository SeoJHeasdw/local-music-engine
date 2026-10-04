"""Run local analysis in its own Python environment and release models on exit."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime" / "quality"
TERMINATE_GRACE_SECONDS = 0.5
KILL_DRAIN_SECONDS = 1.0


def _stop_process_group(child: subprocess.Popen) -> None:
    """Stop every owned descendant, even after the session leader has exited.

    A grandchild can keep output pipes open after its parent exits. Neither
    escalation nor pipe draining may depend on the leader still being alive.
    """
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.communicate(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # Draining pipes does not prove every descendant exited: a detached-output
    # helper may have ignored SIGTERM while the leader was already gone.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        child.communicate(timeout=KILL_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        # A descendant that deliberately left the group may still hold pipes.
        # Closing our ends keeps cancellation bounded; reap the owned leader
        # without an unbounded communicate()/wait() call.
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()
        try:
            child.wait(timeout=KILL_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass


class LocalQualityBackend:
    def __init__(self, *, auto_setup: bool = True):
        self.auto_setup = auto_setup
        self.setup_error: str | None = None

    def prepare(self, progress: Callable[[str], None] | None = None) -> bool:
        if (RUNTIME / "ready.json").is_file() and (RUNTIME / ".venv/bin/python").is_file():
            return True
        if not self.auto_setup:
            self.setup_error = "Local lyric inspection is not installed"
            return False
        if self.setup_error:
            return False
        try:
            if progress:
                progress("quality_setup: Preparing local analysis")
            setup_progress = (lambda message: progress("quality_setup: " + message)) if progress else None
            self._run(["/bin/bash", str(ROOT / "scripts/bootstrap_quality.sh")], timeout=1800, progress=setup_progress,
                      json_output=False)
            return (RUNTIME / "ready.json").is_file()
        except Exception as error:
            self.setup_error = f"{type(error).__name__}: {error}"
            return False

    def analyze(self, audio: Path, *, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        if not self.prepare(progress):
            return {"available": False, "reliable": False, "text": "", "segments": [], "loudness": self.measure_loudness(audio),
                    "error": self.setup_error or "Local lyric inspection is unavailable"}
        try:
            report = self._run([str(RUNTIME / ".venv/bin/python"), str(ROOT / "scripts/quality_worker.py"),
                                "analyze", "--audio", str(audio)], timeout=1800, progress=progress)
            if not isinstance(report, dict):
                raise ValueError("Local analysis returned an invalid report")
            return report
        except Exception as error:
            return {"available": False, "reliable": False, "text": "", "segments": [], "loudness": self.measure_loudness(audio),
                    "error": f"{type(error).__name__}: {error}"}

    @staticmethod
    def measure_loudness(audio: Path) -> dict[str, Any]:
        try:
            result = LocalQualityBackend._run([sys.executable, str(ROOT / "scripts/quality_worker.py"),
                                               "measure", "--audio", str(audio)], timeout=180)
            return result["loudness"]
        except Exception as error:
            return {"status": "unknown", "error": f"{type(error).__name__}: {error}"}

    @staticmethod
    def _run(command: list[str], *, timeout: float, progress: Callable[[str], None] | None = None,
             json_output: bool = True) -> Any:
        environment = dict(os.environ, HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1", PYTHONUNBUFFERED="1",
                           MUSIC_ENGINE_QUALITY_PARENT_PID=str(os.getpid()))
        child = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        deadline = time.monotonic() + timeout
        last_log = ""
        try:
            while True:
                try:
                    stdout, stderr = child.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired as pending:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Local quality inspection timed out")
                    log = (pending.stderr or b"").decode("utf-8", errors="replace")
                    if log != last_log:
                        last_log = log
                        lines = [line.strip() for line in log.splitlines() if line.strip()]
                        if progress and lines:
                            progress(lines[-1][:200])
            if child.returncode:
                raise RuntimeError(stderr.decode("utf-8", errors="replace")[-1500:] or "Local inspection failed")
            if len(stdout) > 4_000_000:
                raise ValueError("Local quality report is too large")
            return json.loads(stdout) if json_output else None
        finally:
            _stop_process_group(child)


class AudioOnlyBackend:
    def prepare(self, progress=None) -> bool:
        return False

    def analyze(self, audio: Path, *, progress=None) -> dict[str, Any]:
        return {"available": False, "reliable": False, "text": "", "segments": [],
                "loudness": self.measure_loudness(audio),
                "error": "Lyric inspection is disabled for this request"}

    measure_loudness = staticmethod(LocalQualityBackend.measure_loudness)
