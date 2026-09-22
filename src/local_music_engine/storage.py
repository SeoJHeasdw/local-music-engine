"""Atomic JSON project storage with immutable artifact verification."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
import fcntl
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import validate_job_transition

SCHEMA_VERSION = 1
PROJECT_FILENAME = "project.json"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class ProjectStore:
    """Own the durable project manifest and files below one project directory."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.manifest_path = self.root / PROJECT_FILENAME

    @classmethod
    def initialize(
        cls,
        root: Path | str,
        *,
        title: str,
        lyrics: str,
        style_prompt: str,
        target_duration_seconds: float,
        structure: str | None = None,
    ) -> "ProjectStore":
        store = cls(root)
        if store.manifest_path.exists():
            raise FileExistsError(f"project already exists: {store.manifest_path}")
        store.root.mkdir(parents=True, exist_ok=True)
        created_at = utc_now()
        project = {
            "schemaVersion": SCHEMA_VERSION,
            "projectId": new_id("project"),
            "title": title.strip() or "Untitled",
            "createdAt": created_at,
            "updatedAt": created_at,
            "inputs": {
                "lyricsOriginal": lyrics,
                "lyricsNormalized": lyrics.replace("\r\n", "\n").replace("\r", "\n"),
                "stylePrompt": style_prompt,
                "targetDurationSeconds": float(target_duration_seconds),
                "structure": structure,
            },
            "selectedCandidateId": None,
            "requests": [],
            "artifacts": [],
            "findings": [],
            "candidates": [],
            "jobs": [],
            "revisions": [],
        }
        store.save(project)
        return store

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise FileNotFoundError(f"project not found: {self.manifest_path}") from error
        if data.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported project schema: {data.get('schemaVersion')!r}; "
                f"expected {SCHEMA_VERSION}"
            )
        required = {
            "projectId",
            "inputs",
            "requests",
            "artifacts",
            "findings",
            "candidates",
            "jobs",
            "revisions",
        }
        missing = required.difference(data)
        if missing:
            raise ValueError(f"project is missing fields: {', '.join(sorted(missing))}")
        return data

    @contextmanager
    def locked(self):
        """Serialize project mutations without making the lock file project state."""

        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".project.lock"
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save(self, project: dict[str, Any]) -> None:
        project = deepcopy(project)
        project["updatedAt"] = utc_now()
        atomic_write_json(self.manifest_path, project)

    def relative_path(self, path: Path | str) -> str:
        resolved = Path(path).expanduser().resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError as error:
            raise ValueError(f"artifact must be inside project: {resolved}") from error

    def resolve_artifact(self, artifact: dict[str, Any]) -> Path:
        candidate = (self.root / artifact["path"]).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"artifact escapes project root: {artifact['path']}") from error
        return candidate

    def verify_artifact(self, artifact: dict[str, Any]) -> tuple[bool, str]:
        path = self.resolve_artifact(artifact)
        if not path.is_file():
            return False, f"missing artifact file: {path}"
        actual_size = path.stat().st_size
        if actual_size != artifact["bytes"]:
            return False, f"artifact size changed: {path}"
        actual_hash = sha256_file(path)
        if actual_hash != artifact["sha256"]:
            return False, f"artifact hash changed: {path}"
        return True, "ok"

    def append_job(
        self,
        project: dict[str, Any],
        *,
        kind: str,
        parameters: dict[str, Any],
        parent_job_id: str | None = None,
    ) -> dict[str, Any]:
        job = {
            "jobId": new_id("job"),
            "kind": kind,
            "status": "queued",
            "stage": "queued",
            "progress": 0.0,
            "parameters": deepcopy(parameters),
            "parentJobId": parent_job_id,
            "remoteTaskId": None,
            "resultRefs": [],
            "error": None,
            "cancelRequested": False,
            "createdAt": utc_now(),
            "startedAt": None,
            "finishedAt": None,
        }
        project["jobs"].append(job)
        return job

    def transition_job(
        self,
        job: dict[str, Any],
        status: str,
        *,
        stage: str | None = None,
        progress: float | None = None,
        error: str | None = None,
    ) -> None:
        validate_job_transition(job["status"], status)
        job["status"] = status
        if status == "running" and job["startedAt"] is None:
            job["startedAt"] = utc_now()
        if status in {"succeeded", "partial", "failed", "cancelled"}:
            job["finishedAt"] = utc_now()
        if stage is not None:
            job["stage"] = stage
        if progress is not None:
            job["progress"] = max(0.0, min(1.0, float(progress)))
        if error is not None:
            job["error"] = error

    @staticmethod
    def find_by_id(project: dict[str, Any], collection: str, key: str, value: str) -> dict[str, Any]:
        for record in project[collection]:
            if record.get(key) == value:
                return record
        raise KeyError(f"{collection} record not found: {value}")
