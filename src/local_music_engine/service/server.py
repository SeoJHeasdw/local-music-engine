"""Local HTTP API of the job service, bound to 127.0.0.1.

:class:`Api` answers a request without a socket, so tests call it directly. The
standard library serves it; the service adds no dependency to the engine.
"""

from __future__ import annotations

import json
import re
import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from ..engines import ACE_BASE_URL, ACE_ENGINE, MUSIC3_BASE_URL, MUSIC3_ENGINE
from . import CONTRACT_VERSION, DEFAULT_PORT, ENGINE, default_output_dir
from .jobs import FINDING_REVIEW_STATES, REVIEW_STATES, JobNotFound, JobStore, now_iso
from .recipes import InvalidInput
from .runner import EngineStopping, Runner

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
MAX_BODY_BYTES = 1 << 20
MAX_LIST = 500
# 모델을 쥔 채 떠 있는 음악 서버. 작업이 없어도 그 메모리는 다른 엔진이 쓸 수 없다.
MODEL_SERVERS = ((ACE_ENGINE, ACE_BASE_URL), (MUSIC3_ENGINE, MUSIC3_BASE_URL))
_JOB = r"(?P<job>[^/]+)"


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def resident_model_server(servers=MODEL_SERVERS, timeout: float = 0.2) -> str | None:
    """``ace-step:18001`` while a music model server listens at its default address."""

    for name, url in servers:
        address = urlsplit(url)
        try:
            with socket.create_connection((address.hostname, address.port), timeout=timeout):
                return f"{name}:{address.port}"
        except OSError:
            continue
    return None


def _hostname(value: str) -> str:
    try:
        parsed = urlsplit(f"http://{value}")
        parsed.port  # noqa: B018 - a malformed port raises here
        if parsed.username is not None or parsed.path or parsed.query or parsed.fragment:
            return ""
        return parsed.hostname or ""
    except ValueError:
        return ""


def _same_origin(origin: str, host: str, port: int) -> bool:
    try:
        given = urlsplit(origin)
        return (given.scheme == "http" and given.hostname in LOCAL_HOSTS and given.hostname == host
                and (given.port or 80) == port and not given.username and not given.path
                and not given.query and not given.fragment)
    except ValueError:
        return False


def _object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "본문은 JSON 객체여야 합니다.") from None
    if not isinstance(value, dict):
        raise ApiError(400, "본문은 JSON 객체여야 합니다.")
    return value


class Api:
    def __init__(self, store: JobStore, runner: Runner, *, port: int = DEFAULT_PORT,
                 resident: Callable[[], str | None] = resident_model_server):
        self.store = store
        self.runner = runner
        self.port = port
        self.resident = resident
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Any], int]] = [
            ("GET", re.compile(r"/api/health"), self.health, 200),
            ("GET", re.compile(r"/api/recipes"), self.recipes, 200),
            ("GET", re.compile(r"/api/jobs"), self.list_jobs, 200),
            ("POST", re.compile(r"/api/jobs"), self.create_job, 201),
            ("GET", re.compile(rf"/api/jobs/{_JOB}"), self.get_job, 200),
            ("POST", re.compile(rf"/api/jobs/{_JOB}/cancel"), self.cancel_job, 200),
            ("POST", re.compile(rf"/api/jobs/{_JOB}/assets/(?P<asset>[^/]+)/review"), self.review_asset, 200),
            ("POST", re.compile(rf"/api/jobs/{_JOB}/findings/(?P<finding>[^/]+)/review"), self.review_finding, 200),
        ]

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes = b"") -> tuple[int, Any]:
        try:
            self._guard(headers)
            url = urlsplit(target)
            allowed = False
            for route_method, pattern, handler, status in self._routes:
                match = pattern.fullmatch(url.path)
                if match is None:
                    continue
                allowed = True
                if route_method != method:
                    continue
                query = {key: values[-1] for key, values in parse_qs(url.query).items()}
                return status, handler(**match.groupdict(), query=query, body=body)
            raise ApiError(405 if allowed else 404, "지원하지 않는 요청입니다." if allowed else "찾을 수 없습니다.")
        except ApiError as error:
            return error.status, {"detail": error.detail}
        except Exception as error:  # noqa: BLE001 - every error answers {detail}
            return 500, {"detail": f"{type(error).__name__}: {error}"}

    def _guard(self, headers: Mapping[str, str]) -> None:
        # 다른 호스트 이름(DNS 리바인딩)과 다른 출처의 브라우저 요청을 막는다. Origin 없는 로컬 CLI는 받는다.
        host = _hostname(headers.get("host", ""))
        if host not in LOCAL_HOSTS:
            raise ApiError(403, "로컬 요청만 받습니다.")
        origin = headers.get("origin")
        if origin is not None and not _same_origin(origin, host, self.port):
            raise ApiError(403, "다른 출처의 요청은 받지 않습니다.")
        if headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            raise ApiError(403, "다른 사이트의 요청은 받지 않습니다.")

    def health(self, **_: Any) -> dict[str, Any]:
        current = self.runner.current_job_id
        working = None if current else self.runner.busy_by()
        # 일이 없는데 모델 서버만 떠 있으면 상주다. 메모리는 쥐고 있으므로 busy는 참이다.
        resident = None if current or working else self.resident()
        holder = working or resident
        return {"ok": True, "engine": ENGINE, "version": __version__, "contract": CONTRACT_VERSION,
                "busy": bool(current or holder), **({"busyBy": holder} if holder else {}),
                "resident": resident is not None, "currentJob": current, "outputDir": str(self.store.root.parent)}

    def recipes(self, **_: Any) -> dict[str, Any]:
        return {"recipes": self.runner.describe()}

    def list_jobs(self, *, query: dict[str, str], **_: Any) -> dict[str, Any]:
        try:
            limit = int(query.get("limit", "100"))
        except ValueError:
            raise ApiError(400, "limit은 정수여야 합니다.") from None
        return {"jobs": self.store.list(limit=max(1, min(limit, MAX_LIST)))}

    def create_job(self, *, body: bytes, **_: Any) -> dict[str, Any]:
        payload = _object(body)
        params = payload.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ApiError(400, "params는 설정 객체여야 합니다.")
        try:
            return self.runner.submit(payload.get("recipe"), params)
        except InvalidInput as error:
            raise ApiError(400, str(error)) from error
        except EngineStopping as error:
            raise ApiError(409, str(error)) from error

    def _job(self, job: str) -> dict[str, Any]:
        try:
            return self.store.load(job)
        except JobNotFound:
            raise ApiError(404, "작업을 찾을 수 없습니다.") from None

    def get_job(self, *, job: str, **_: Any) -> dict[str, Any]:
        return self._job(job)

    def cancel_job(self, *, job: str, **_: Any) -> dict[str, Any]:
        self._job(job)
        try:
            return self.runner.cancel(job)
        except JobNotFound:
            raise ApiError(404, "작업을 찾을 수 없습니다.") from None

    def review_asset(self, *, job: str, asset: str, body: bytes, **_: Any) -> dict[str, Any]:
        """The person's judgment of one result, bound to the file as it is now."""

        status = _object(body).get("status")
        if not isinstance(status, str) or status not in REVIEW_STATES:
            raise ApiError(400, f"status는 {sorted(REVIEW_STATES)} 중 하나여야 합니다.")
        record = next((item for item in self._job(job)["assets"] if item["id"] == asset), None)
        if record is None:
            raise ApiError(404, "에셋을 찾을 수 없습니다.")
        digest = None
        if status != "pending":
            try:
                digest = self.store.file_sha256(self.store.resolve_file(job, record["file"]))
            except (JobNotFound, OSError):
                raise ApiError(409, "에셋 파일이 없어 판단을 기록할 수 없습니다.") from None

        def apply(record: dict[str, Any]) -> None:
            target = next((item for item in record["assets"] if item["id"] == asset), None)
            if target is None:
                raise JobNotFound(asset)
            target.update(review=status, reviewedAt=now_iso())
            if digest is None:
                target.pop("reviewedSha256", None)
            else:
                target["reviewedSha256"] = digest

        try:
            return self.store.update(job, apply)
        except JobNotFound:
            raise ApiError(404, "에셋을 찾을 수 없습니다.") from None

    def review_finding(self, *, job: str, finding: str, body: bytes, **_: Any) -> dict[str, Any]:
        """Whether a person checked that spot. Automatic checks never change this."""

        status = _object(body).get("status")
        if not isinstance(status, str) or status not in FINDING_REVIEW_STATES:
            raise ApiError(400, f"status는 {sorted(FINDING_REVIEW_STATES)} 중 하나여야 합니다.")
        self._job(job)

        def apply(record: dict[str, Any]) -> None:
            target = next((item for item in record["findings"] if item["id"] == finding), None)
            if target is None:
                raise JobNotFound(finding)
            target.update(review=status, reviewedAt=now_iso())

        try:
            return self.store.update(job, apply)
        except JobNotFound:
            raise ApiError(404, "확인할 항목을 찾을 수 없습니다.") from None


def _handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "local-music-engine"
        protocol_version = "HTTP/1.1"

        def _serve(self) -> None:
            length = self.headers.get("content-length")
            try:
                size = int(length) if length else 0
            except ValueError:
                size = -1
            if size < 0 or size > MAX_BODY_BYTES:
                status, payload = 413, {"detail": "요청 본문이 너무 큽니다."}
                self.close_connection = True
            else:
                body = self.rfile.read(size) if size else b""
                headers = {key.lower(): value for key, value in self.headers.items()}
                status, payload = api.handle(self.command, self.path, headers, body)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

    return Handler


def serve(*, port: int = DEFAULT_PORT, output_dir: Path | None = None) -> int:
    """Run until SIGINT/SIGTERM. Prints one JSON document when it is listening."""

    store = JobStore((output_dir or default_output_dir()) / "jobs")
    store.claim()
    runner = Runner(store)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _handler(Api(store, runner, port=port)))
    except OSError:
        store.release()
        raise
    server.daemon_threads = True
    runner.start()
    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    previous = {signum: signal.signal(signum, request_stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    thread = threading.Thread(target=server.serve_forever, name="music-service-http", daemon=True)
    thread.start()
    print(json.dumps({"service": f"http://127.0.0.1:{port}", "engine": ENGINE, "contract": CONTRACT_VERSION,
                      "outputDir": str(store.root.parent)}, ensure_ascii=False), flush=True)
    try:
        while not stop.wait(0.5):
            pass
    finally:
        # 실행 중이던 작업을 failed · interrupted로 닫고 나서 연결을 끊는다.
        runner.shutdown()
        server.shutdown()
        server.server_close()
        store.release()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print("music engine service stopped", file=sys.stderr)
    return 0
