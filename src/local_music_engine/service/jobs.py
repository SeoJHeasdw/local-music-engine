"""Service job records: one folder per job under ``<output>/jobs``, ``job.json`` is the record.

These records belong to the service queue. A song's own history stays in its
``project.json``. Only the service process holding the store's ``.service.lock``
writes here, so one in-process lock orders the runner thread and API requests.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from ..storage import atomic_write_json, sha256_file

JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
ACTIVE_STATES = frozenset({"queued", "running", "cancelling"})
REVIEW_STATES = frozenset({"pending", "approved", "rejected"})
FINDING_REVIEW_STATES = frozenset({"pending", "cleared"})
ERROR_CODES = frozenset({"invalid_input", "out_of_memory", "interrupted", "engine_error"})
# 엔진이 멈추거나 다시 시작하며 끝낸 작업은 failed · interrupted다. cancelled는 취소 요청에만 쓴다.
RESTARTED_ERROR = "서비스가 다시 시작되어 작업이 중단됐습니다."
RESTARTED_QUEUED_ERROR = "서비스가 다시 시작되어 대기 중이던 작업을 실행하지 않고 닫았습니다."
STOPPED_ERROR = "서비스가 종료되어 작업이 중단됐습니다."
STOPPED_QUEUED_ERROR = "서비스가 종료되어 대기 중이던 작업을 실행하지 않고 닫았습니다."


class JobNotFound(KeyError):
    pass


class StoreBusy(RuntimeError):
    """Another service already owns this output folder."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


class JobStore:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._owner_fd: int | None = None
        # 승인을 묶은 파일의 SHA-256. 크기와 수정 시각이 같으면 다시 해시하지 않는다.
        self._digests: dict[tuple[str, int, int], str] = {}

    def claim(self) -> None:
        """Own this output folder for the life of the process; a second service is refused."""

        fd = os.open(self.root / ".service.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise StoreBusy(f"another music engine service already uses {self.root}") from None
        self._owner_fd = fd

    def release(self) -> None:
        if self._owner_fd is not None:
            os.close(self._owner_fd)
            self._owner_fd = None

    def job_dir(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise JobNotFound(job_id)
        path = self.root / job_id
        if path.is_symlink():
            raise JobNotFound(job_id)
        return path

    def create(self, recipe: str, params: dict[str, Any], title: str) -> dict[str, Any]:
        with self._lock:
            while True:
                job_id = new_job_id()
                try:
                    self.job_dir(job_id).mkdir()
                    break
                except FileExistsError:
                    continue
            job = {
                "id": job_id, "recipe": recipe, "title": title, "params": deepcopy(params),
                "state": "queued", "createdAt": now_iso(), "startedAt": None, "finishedAt": None,
                "stages": [], "assets": [], "findings": [],
                "error": None, "errorCode": None, "logTail": [],
            }
            atomic_write_json(self.job_dir(job_id) / "job.json", job)
            return self._present(job)

    def _read(self, job_id: str) -> dict[str, Any]:
        path = self.job_dir(job_id) / "job.json"
        try:
            if path.is_symlink():
                raise JobNotFound(job_id)
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise JobNotFound(job_id) from error
        if not isinstance(job, dict) or job.get("id") != job_id:
            raise JobNotFound(job_id)
        return job

    def load(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._present(self._read(job_id))

    def update(self, job_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            job = self._read(job_id)
            mutate(job)
            atomic_write_json(self.job_dir(job_id) / "job.json", job)
            return self._present(job)

    def list(self, limit: int | None = 100) -> list[dict[str, Any]]:
        jobs = []
        for job_id in self._ids():
            if limit is not None and len(jobs) >= limit:
                break
            try:
                jobs.append(self.load(job_id))
            except JobNotFound:
                continue
        return jobs

    def _ids(self) -> list[str]:
        return sorted((entry.name for entry in self.root.iterdir()
                       if JOB_ID.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()),
                      reverse=True)

    def resolve_file(self, job_id: str, relative: str) -> Path:
        base = self.job_dir(job_id).resolve()
        target = (base / relative).resolve()
        if base not in target.parents or not target.is_file():
            raise JobNotFound(f"{job_id}/{relative}")
        return target

    def file_sha256(self, path: Path) -> str:
        info = path.stat()
        key = (str(path), info.st_size, info.st_mtime_ns)
        digest = self._digests.get(key)
        if digest is None:
            digest = sha256_file(path)
            self._digests[key] = digest
        return digest

    def _present(self, job: dict[str, Any]) -> dict[str, Any]:
        """The record as the API shows it: ``dir`` attached, stale approvals withheld."""

        shown = deepcopy(job)
        shown["dir"] = str(self.root / job["id"])
        for asset in shown.get("assets", []):
            reviewed = asset.get("reviewedSha256")
            if asset.get("review") == "pending" or not reviewed:
                continue
            try:
                current = self.file_sha256(self.resolve_file(job["id"], asset["file"]))
            except (JobNotFound, OSError):
                current = None
            if current != reviewed:
                # 사람의 판단은 그때의 파일에 묶인다. 파일이 바뀌거나 사라지면 그 판단을 내보내지 않는다.
                asset["review"] = "pending"
                asset.pop("reviewedSha256", None)
                asset.pop("reviewedAt", None)
        return shown

    def recover_interrupted(self) -> list[str]:
        """Close what a stopped service left open. Nothing restarts on its own."""

        closed = []
        for job_id in self._ids():
            def close(job: dict[str, Any]) -> None:
                if job["state"] not in ACTIVE_STATES:
                    return
                if job["state"] == "cancelling":
                    # 멈추기 전에 취소 요청이 왔으면 사람이 취소한 작업이다.
                    job.update(state="cancelled", finishedAt=now_iso())
                    stage_state = "cancelled"
                else:
                    queued = job["state"] == "queued"
                    job.update(state="failed", errorCode="interrupted", finishedAt=now_iso(),
                               error=RESTARTED_QUEUED_ERROR if queued else RESTARTED_ERROR)
                    stage_state = "failed"
                for stage in job["stages"]:
                    if stage.get("state") == "running":
                        stage["state"] = stage_state
                closed.append(job_id)

            try:
                if self._read(job_id)["state"] in ACTIVE_STATES:
                    self.update(job_id, close)
            except JobNotFound:
                continue
        return closed

    def discard_finished(self, recipes: Iterable[str]) -> int:
        """Delete finished jobs whose output is not a result (``selftest``)."""

        names = frozenset(recipes)
        removed = 0
        with self._lock:
            for job_id in self._ids():
                try:
                    job = self._read(job_id)
                except JobNotFound:
                    continue
                if job["recipe"] in names and job["state"] not in ACTIVE_STATES:
                    shutil.rmtree(self.job_dir(job_id))
                    removed += 1
        return removed
