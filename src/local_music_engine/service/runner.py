"""Single-lane job runner: one job at a time, and never beside a CLI generation.

A submitted job waits ``queued`` until it holds the machine's music work lane
exclusively (:mod:`..lane`). The CLI's generation, cover, repaint and resume runs
hold the same lane shared, so the app and terminal runs never overlap a service job.
"""

from __future__ import annotations

import collections
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from ..lane import ExclusiveLane
from .jobs import STOPPED_ERROR, STOPPED_QUEUED_ERROR, JobNotFound, JobStore, now_iso
from .recipes import RECIPES, InvalidInput, Recipe

FLUSH_INTERVAL_S = 0.25
LANE_POLL_S = 0.2
WAIT_SLICE_S = 0.05
LOG_TAIL_LINES = 40
FINDING_SEVERITIES = frozenset({"fail", "warn"})


class Cancelled(Exception):
    """A cancel request (or the service stopping) reached a running job."""


class EngineStopping(RuntimeError):
    """The service is shutting down and takes no new jobs."""


class Stage:
    def __init__(self, ctx: "JobContext", name: str, label: str):
        self.ctx = ctx
        self.name = name
        self.label = label
        self.fraction = 0.0
        self.detail: str | None = None
        self._index = -1
        self._started = 0.0
        self._last_flush = 0.0

    def __enter__(self) -> "Stage":
        self.ctx.check_cancel()
        self._started = time.monotonic()

        def add(job: dict[str, Any]) -> None:
            job["stages"].append({
                "name": self.name, "label": self.label, "state": "running", "startedAt": now_iso(),
                "progress": 0.0, "detail": None, "seconds": None, "peakMemoryBytes": None,
            })

        self._index = len(self.ctx.mutate(add)["stages"]) - 1
        return self

    def progress(self, fraction: float, detail: str | None = None, *, force: bool = False) -> None:
        self.fraction = max(self.fraction, min(1.0, max(0.0, fraction)))
        if detail:
            self.detail = detail
        now = time.monotonic()
        if force or now - self._last_flush >= FLUSH_INTERVAL_S:
            self._last_flush = now
            self._write("running")

    def wait(self, seconds: float) -> None:
        """Spend ``seconds`` here, reporting progress and stopping at a cancel request."""

        started = time.monotonic()
        while True:
            self.ctx.check_cancel()
            elapsed = time.monotonic() - started
            if elapsed >= seconds:
                return
            self.progress(elapsed / seconds if seconds else 1.0)
            self.ctx.cancel.wait(min(WAIT_SLICE_S, seconds - elapsed))

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            state, self.fraction = "done", 1.0
        elif issubclass(exc_type, Cancelled):
            state = "cancelled"
        else:
            state = "failed"
        self._write(state, final=True)
        return False

    def _write(self, state: str, *, final: bool = False) -> None:
        def apply(job: dict[str, Any]) -> None:
            stage = job["stages"][self._index]
            # 취소 요청 없이 멈춘 것은 서비스가 끊은 것이다. 작업과 같이 실패로 적는다.
            shown = "failed" if state == "cancelled" and job["state"] != "cancelling" else state
            stage.update(state=shown, progress=round(self.fraction, 4), detail=self.detail)
            if final:
                # 이 단계들은 서비스 프로세스 안에서 돌며 모델을 올리지 않는다. 잴 단계 프로세스가 없다.
                stage.update(seconds=round(time.monotonic() - self._started, 3), peakMemoryBytes=None)

        self.ctx.mutate(apply)


class JobContext:
    def __init__(self, store: JobStore, job: dict[str, Any], cancel: threading.Event):
        self.store = store
        self.job_id = job["id"]
        self.params: dict[str, Any] = job["params"]
        self.dir = store.job_dir(self.job_id)
        self.cancel = cancel
        self._tail: collections.deque[str] = collections.deque(maxlen=LOG_TAIL_LINES)

    def mutate(self, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        return self.store.update(self.job_id, fn)

    def stage(self, name: str, label: str) -> Stage:
        return Stage(self, name, label)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def log(self, text: str) -> None:
        lines = [line for line in text.splitlines() if line.strip()]
        with (self.dir / "job.log").open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
        self._tail.extend(line[-500:] for line in lines)
        tail = list(self._tail)
        self.mutate(lambda job: job.__setitem__("logTail", tail))

    def rel(self, path: str | Path) -> str:
        return Path(path).resolve().relative_to(self.dir.resolve()).as_posix()

    def add_asset(self, *, kind: str, role: str, file: str | Path,
                  meta: dict[str, Any] | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {}

        def add(job: dict[str, Any]) -> None:
            record.update({"id": f"a{len(job['assets']) + 1:02d}", "kind": kind, "role": role,
                           "file": self.rel(file), "meta": meta or {}, "review": "pending",
                           "createdAt": now_iso()})
            job["assets"].append(dict(record))

        self.mutate(add)
        return record

    def add_finding(self, *, severity: str, code: str, where: str, params: dict[str, Any] | None = None,
                    asset_id: str | None = None, evidence: tuple[str | Path, ...] | list[str | Path] = ()) -> dict[str, Any]:
        """One place a person should look. It never changes an asset's review."""

        if severity not in FINDING_SEVERITIES:
            raise ValueError(f"finding severity must be one of {sorted(FINDING_SEVERITIES)}")
        record: dict[str, Any] = {}

        def add(job: dict[str, Any]) -> None:
            record.update({"id": f"f{len(job['findings']) + 1:02d}", "severity": severity, "code": code,
                           "params": params or {}, "where": where, "assetId": asset_id,
                           "evidence": [self.rel(path) for path in evidence], "review": "pending"})
            job["findings"].append(dict(record))

        self.mutate(add)
        return record


class Runner:
    def __init__(self, store: JobStore, *, recipes: dict[str, Recipe] | None = None,
                 lane: ExclusiveLane | None = None):
        self.store = store
        self.recipes = RECIPES if recipes is None else recipes
        self.lane = lane or ExclusiveLane()
        self.current_job_id: str | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._cancels: dict[str, threading.Event] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.store.recover_interrupted()
        # 점검 작업은 결과물이 아니다. 계약 검사가 남긴 기록과 폴더를 시작할 때 지운다.
        self.store.discard_finished(name for name, recipe in self.recipes.items() if recipe.diagnostic)
        self._thread = threading.Thread(target=self._loop, name="music-job-runner", daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 8.0) -> None:
        """Stop taking jobs and stop the running one.

        Jobs left unfinished end ``failed`` · ``interrupted``: nobody asked to cancel
        them, and a client resubmits only the jobs the engine cut off.
        """

        self._stopping.set()
        with self._lock:
            pending = list(self._pending)
            events = list(self._cancels.values())
        for job_id in pending:
            self._close_queued(job_id)
        for event in events:
            event.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def describe(self) -> list[dict[str, Any]]:
        return [recipe.describe() for recipe in self.recipes.values()]

    def submit(self, recipe_name: Any, params: dict[str, Any]) -> dict[str, Any]:
        recipe = self.recipes.get(recipe_name) if isinstance(recipe_name, str) else None
        if recipe is None:
            raise InvalidInput(f"알 수 없는 레시피입니다: {recipe_name!r}")
        normalized, title = recipe.prepare(dict(params))
        with self._lock:
            if self._stopping.is_set():
                raise EngineStopping("서비스가 종료 중이라 새 작업을 받지 않습니다.")
            job = self.store.create(recipe.name, normalized, title)
            self._pending.add(job["id"])
        self._queue.put(job["id"])
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        def mark(job: dict[str, Any]) -> None:
            if job["state"] == "queued":
                job.update(state="cancelled", finishedAt=now_iso())
            elif job["state"] == "running":
                job["state"] = "cancelling"

        with self._lock:
            job = self.store.update(job_id, mark)
            if event := self._cancels.get(job_id):
                event.set()
        return job

    def busy_by(self) -> str | None:
        """Who uses the lane without a job record here: a CLI or app generation."""

        if self.current_job_id is not None or self.lane.held:
            return None
        return "lane:generation.lock" if self.lane.held_by_other() else None

    def _close_queued(self, job_id: str) -> None:
        def close(job: dict[str, Any]) -> None:
            if job["state"] == "queued":
                job.update(state="failed", error=STOPPED_QUEUED_ERROR, errorCode="interrupted", finishedAt=now_iso())

        try:
            self.store.update(job_id, close)
        except JobNotFound:
            pass

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                job_id = self._queue.get(timeout=LANE_POLL_S)
            except queue.Empty:
                continue
            try:
                self._run(job_id)
            except Exception:  # noqa: BLE001 - keep the lane serving the next job
                traceback.print_exc()
            finally:
                with self._lock:
                    self._pending.discard(job_id)

    def _run(self, job_id: str) -> None:
        while True:
            try:
                job = self.store.load(job_id)
            except JobNotFound:
                return
            if job["state"] != "queued":
                return
            if self._stopping.is_set():
                self._close_queued(job_id)
                return
            if self.lane.try_acquire():
                break
            # CLI·앱의 생성이 차선을 쥐고 있다. 끝날 때까지 queued로 기다린다.
            self._stopping.wait(LANE_POLL_S)
        try:
            self._run_in_lane(job_id)
        finally:
            self.lane.release()

    def _run_in_lane(self, job_id: str) -> None:
        cancel = threading.Event()
        with self._lock:
            self._cancels[job_id] = cancel
            self.current_job_id = job_id
            if self._stopping.is_set():
                cancel.set()
        claimed = False
        ctx: JobContext | None = None
        state, error, code = "done", None, None
        try:
            def claim(record: dict[str, Any]) -> None:
                if record["state"] == "queued":
                    record.update(state="running", startedAt=now_iso())

            job = self.store.update(job_id, claim)
            claimed = job["state"] == "running"
            if not claimed:
                return
            ctx = JobContext(self.store, job, cancel)
            self.recipes[job["recipe"]].run(ctx)
            ctx.check_cancel()
        except Cancelled:
            state = "cancelled"
            # 서비스가 멈추며 끊은 실행은 취소가 아니다. 그 전에 취소 요청이 왔으면 취소로 둔다.
            if self._stopping.is_set() and self.store.load(job_id)["state"] != "cancelling":
                state, error, code = "failed", STOPPED_ERROR, "interrupted"
        except InvalidInput as exc:
            state, error, code = "failed", str(exc), "invalid_input"
        except MemoryError as exc:
            state, error, code = "failed", f"MemoryError: {exc}", "out_of_memory"
        except Exception as exc:  # noqa: BLE001 - every failure must land in job.json
            state, error, code = "failed", str(exc) or type(exc).__name__, "engine_error"
            if ctx is not None:
                try:
                    ctx.log(traceback.format_exc())
                except Exception:  # noqa: BLE001 - logging must not skip the final record
                    pass
        finally:
            # currentJob을 먼저 비운다. 끝난 기록을 읽은 쪽이 health에서 그 작업을 다시 보지 않게 한다.
            with self._lock:
                self._cancels.pop(job_id, None)
                self.current_job_id = None
            if claimed:
                self.store.update(job_id, lambda record: record.update(
                    state=state, error=error, errorCode=code, finishedAt=now_iso()))
