"""Private credentials shared by the managed ACE server, CLI and Electron main."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path


def api_key_path() -> Path:
    configured = os.environ.get("MUSIC_ENGINE_ACE_API_KEY_FILE")
    return Path(configured).expanduser().absolute() if configured else Path(__file__).resolve().parents[2] / ".runtime" / "ace-api-key"


def validate_api_key(value: str) -> str:
    key = value.strip()
    if not key or len(key) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("ACE API credential is empty or invalid")
    return key


def configured_api_key() -> str | None:
    for name in ("MUSIC_ENGINE_ACE_API_KEY", "ACESTEP_API_KEY"):
        if name in os.environ:
            return validate_api_key(os.environ[name])
    return None


def read_api_key() -> str | None:
    configured = configured_api_key()
    if configured is not None:
        return configured
    try:
        descriptor = os.open(api_key_path(), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None  # An externally owned, unauthenticated server remains usable.
    with os.fdopen(descriptor, "r", encoding="ascii") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
            raise ValueError("ACE API credential file must be private and owned by this user")
        return validate_api_key(handle.read(4097))


def ensure_api_key() -> str:
    existing = read_api_key()
    if existing is not None:
        return existing
    destination = api_key_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".ace-api-key-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(32) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass  # Another starter published its complete credential first.
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    key = read_api_key()
    if key is None:
        raise RuntimeError("ACE API credential could not be published")
    return key
