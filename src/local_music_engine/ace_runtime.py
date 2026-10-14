"""ACE runs only while there is work.

Generation, cover, repaint and resume hold the machine's work lane (:mod:`.lane`) for
their whole run, and the app starts them through the CLI. When nothing answers at
ACE's address, the run's first request starts the server and waits for its models.
That server watches the lane and stops itself once nothing has held it for a moment,
so an idle engine holds no model memory and a crashed run cannot leave it behind.
A server someone started by hand is used as it is and never stopped here.

``ace-start.lock`` orders starting and stopping: a run never reuses a server that is
shutting down, and two runs never load two servers.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .ace_adapter import AceApiError, AceStepClient, AceUnavailable
from .lane import LANE_ENV, lane_held, lane_path

ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = ROOT / "scripts" / "start_ace_api.sh"
LOG_PATH = ROOT / ".runtime" / "logs" / "ace-server.log"
IDLE_ENV = "MUSIC_ENGINE_ACE_STOP_WHEN_IDLE"
# The first start after bootstrap may still download weights; the log says what it is doing.
START_TIMEOUT_S = 3600.0
IDLE_POLL_S = 1.0
IDLE_POLLS = 2
# Model loading does not answer SIGTERM until it finishes; past this the server exits hard.
STOP_GRACE_S = 20.0


def start_lock_path() -> Path:
    return lane_path().with_name("ace-start.lock")


@contextmanager
def start_lock():
    path = start_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _probe(client: AceStepClient) -> dict[str, Any] | None:
    try:
        return client.health()
    except AceUnavailable:
        return None


def _log_tail(lines: int = 4) -> str:
    try:
        with LOG_PATH.open("rb") as handle:
            handle.seek(max(0, LOG_PATH.stat().st_size - 4096))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return " | ".join([line.strip() for line in text.splitlines() if line.strip()][-lines:])


def spawn_server(base_url: str, *, model: str, lm_model: str) -> subprocess.Popen:
    """Start ACE in its own session; it stops itself when the work lane is free."""

    address = urlsplit(base_url)
    if address.path not in ("", "/"):
        raise ValueError("cannot start ACE for an address with a path")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "MUSIC_ENGINE_ACE_HOST": address.hostname or "127.0.0.1",
        "MUSIC_ENGINE_ACE_PORT": str(address.port or 80),
        "MUSIC_ENGINE_ACE_DIT_MODEL": model,
        "MUSIC_ENGINE_ACE_LM_MODEL": lm_model,
        IDLE_ENV: "1",
        LANE_ENV: str(lane_path()),
        "PYTHONUNBUFFERED": "1",
    }
    with LOG_PATH.open("ab") as log:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        log.write(f"\n--- {stamp} 생성 작업이 엔진을 켬 (port {env['MUSIC_ENGINE_ACE_PORT']}, {model}) ---\n".encode())
        log.flush()
        # A new session keeps a terminal's Ctrl-C on this run from reaching the shared server.
        return subprocess.Popen(["/bin/bash", str(START_SCRIPT)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def ensure_ace(base_url: str, *, model: str, lm_model: str, timeout: float = START_TIMEOUT_S, poll: float = 1.0,
               spawn: Callable[..., subprocess.Popen] = spawn_server) -> None:
    """Make ACE answer at ``base_url`` with its models loaded, starting it when nothing answers.

    Call it while holding the work lane, or the new server stops itself at once.
    """

    client = AceStepClient(base_url, request_timeout_seconds=6.0)
    with start_lock():
        process = None if _probe(client) is not None else spawn(base_url, model=model, lm_model=lm_model)
        deadline = time.monotonic() + timeout
        while True:
            health = _probe(client)
            if health is not None:
                if health.get("stage") == "failed":
                    raise AceApiError(f"ACE engine failed to load its models: {_log_tail()}")
                if health.get("models_initialized"):
                    return
            elif process is not None and process.poll() is not None:
                raise AceApiError(f"ACE engine did not start (exit {process.returncode}): {_log_tail()}")
            if time.monotonic() >= deadline:
                raise AceApiError(f"ACE engine did not finish loading within {timeout:.0f}s: {_log_tail()}")
            time.sleep(poll)


class ManagedAceClient(AceStepClient):
    """ACE client whose first request brings the engine up for this run."""

    def __init__(self, base_url: str, *, model: str, lm_model: str, **kwargs: Any):
        super().__init__(base_url, **kwargs)
        self._engine_models = (model, lm_model)
        self._engine_ready = False

    def _request(self, *args: Any, **kwargs: Any) -> bytes:
        if not self._engine_ready:
            ensure_ace(self.base_url, model=self._engine_models[0], lm_model=self._engine_models[1])
            self._engine_ready = True
        return super()._request(*args, **kwargs)


def managed_client_factory(*, model: str, lm_model: str) -> Callable[[str], ManagedAceClient]:
    return lambda base_url: ManagedAceClient(base_url, model=model, lm_model=lm_model)


def stop_when_idle(*, poll: float = IDLE_POLL_S, idle_polls: int = IDLE_POLLS,
                   stop: Callable[[], None] | None = None) -> threading.Thread:
    """Inside a server a run started: stop it once nothing has held the work lane for ``idle_polls`` looks."""

    lane = lane_path()

    def stop_self() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(STOP_GRACE_S)
        os._exit(1)

    def watch() -> None:
        free = 0
        while True:
            time.sleep(poll)
            free = 0 if lane_held(lane) else free + 1
            if free < idle_polls:
                continue
            # Under the start lock no run can be reusing this server or starting another.
            with start_lock():
                if lane_held(lane):
                    free = 0
                    continue
                (stop or stop_self)()
                return

    thread = threading.Thread(target=watch, name="ace-stop-when-idle", daemon=True)
    thread.start()
    return thread
