"""The machine's one music work lane, shared by the CLI and the job service.

Generation, cover, repaint and resume runs (the app starts them through the CLI)
hold it shared, so they still run side by side exactly as before. A job of the
service (``music-engine serve``) holds it exclusively, so it never overlaps any
of them. The kernel drops the lock when its holder dies, so a crash leaves
nothing to clean up. Never unlink the file: a new inode would let a second owner in.
"""

from __future__ import annotations

import fcntl
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

LANE_ENV = "MUSIC_ENGINE_LANE_LOCK"
# health probes the lane by taking it for an instant. A CLI run that meets such a
# probe waits it out; a service job holds the lane for its whole run.
SHARED_WAIT_SECONDS = 2.0


class LaneBusy(RuntimeError):
    """A service job holds the lane."""


def lane_path() -> Path:
    configured = os.environ.get(LANE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    cache = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
    return cache / "local-music-engine" / "generation.lock"


def lane_held(path: Path | None = None) -> bool:
    """True while anyone holds the lane. Never creates the file."""

    try:
        fd = os.open(path or lane_path(), os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # A lane that cannot be checked counts as taken.
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _open(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)


@contextmanager
def shared_lane(*, wait_seconds: float | None = None):
    """Hold the lane alongside other CLI runs; refuse while a service job runs."""

    fd = _open(lane_path())
    try:
        deadline = time.monotonic() + (SHARED_WAIT_SECONDS if wait_seconds is None else wait_seconds)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LaneBusy("the music engine service is running a job; try again when it finishes") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


class ExclusiveLane:
    """The service's side: one job at a time, and no CLI run beside it."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else lane_path()
        self._fd: int | None = None
        self._lock = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        with self._lock:
            if self._fd is not None:
                return True
            fd = _open(self.path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            self._fd = fd
            return True

    def release(self) -> None:
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def held_by_other(self) -> bool:
        """True while a CLI run (or another holder) has the lane. Never creates the file."""

        with self._lock:
            return self._fd is None and lane_held(self.path)
