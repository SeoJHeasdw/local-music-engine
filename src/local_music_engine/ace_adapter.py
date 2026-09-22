"""Small, typed wrapper around the official ACE-Step REST API."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


class AceApiError(RuntimeError):
    pass


def _require_loopback(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("ACE API must use an http loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ACE API URL must not include credentials, query, or fragment")
    return base_url.rstrip("/")


class AceStepClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18001",
        *,
        api_key: str | None = None,
        request_timeout_seconds: float = 30.0,
    ):
        self.base_url = _require_loopback(base_url)
        self.api_key = api_key
        self.request_timeout_seconds = request_timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        if not path.startswith("/"):
            raise ValueError("API path must be relative to the configured server")
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.request_timeout_seconds
            ) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise AceApiError(f"ACE API HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise AceApiError(f"ACE API unavailable: {error.reason}") from error

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        raw = self._request(
            method,
            path,
            body=body,
            content_type="application/json" if body is not None else None,
        )
        decoded = json.loads(raw)
        if isinstance(decoded, dict) and decoded.get("error"):
            raise AceApiError(str(decoded["error"]))
        return decoded

    def health(self) -> dict[str, Any]:
        response = self._json("GET", "/health")
        data = response.get("data")
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise AceApiError(f"unhealthy ACE API response: {response!r}")
        return data

    def submit(self, request: dict[str, Any], source_audio: Path | None = None) -> str:
        if source_audio is None:
            response = self._json("POST", "/release_task", request)
        else:
            content_type, body = _multipart_body(request, source_audio)
            raw = self._request("POST", "/release_task", body=body, content_type=content_type)
            response = json.loads(raw)
        data = response.get("data")
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not task_id:
            raise AceApiError(f"ACE API did not return a task id: {response!r}")
        return str(task_id)

    def query(self, task_id: str) -> dict[str, Any]:
        response = self._json("POST", "/query_result", {"task_id_list": [task_id]})
        rows = response.get("data")
        if not isinstance(rows, list) or not rows:
            raise AceApiError(f"ACE API lost task {task_id}")
        row = rows[0]
        result_raw = row.get("result", "[]")
        try:
            results = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
        except json.JSONDecodeError as error:
            raise AceApiError(f"invalid ACE result payload for {task_id}") from error
        result = results[0] if isinstance(results, list) and results else {}
        status = int(row.get("status", result.get("status", 0)))
        return {
            "taskId": task_id,
            "status": status,
            "progress": float(result.get("progress", 0.0)),
            "stage": result.get("stage") or row.get("progress_text") or "running",
            "result": result,
        }

    def wait(
        self,
        task_id: str,
        *,
        poll_seconds: float = 1.0,
        timeout_seconds: float = 1800.0,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            state = self.query(task_id)
            if on_progress:
                on_progress(state)
            if state["status"] == 1:
                return state["result"]
            if state["status"] == 2:
                raise AceApiError(str(state["result"].get("error") or state["stage"]))
            if time.monotonic() >= deadline:
                raise TimeoutError(f"ACE task timed out: {task_id}")
            time.sleep(max(0.1, poll_seconds))

    def download(self, file_path: str, destination: Path) -> None:
        parsed = urllib.parse.urlparse(file_path)
        if parsed.scheme or parsed.netloc or parsed.path != "/v1/audio":
            raise AceApiError(f"unexpected ACE audio URL: {file_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(
                    self._request(
                        "GET", file_path, timeout=max(120.0, self.request_timeout_seconds)
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            if temporary.stat().st_size == 0:
                raise AceApiError("ACE API returned an empty audio file")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _multipart_body(fields: dict[str, Any], source_audio: Path) -> tuple[str, bytes]:
    if not source_audio.is_file():
        raise FileNotFoundError(source_audio)
    boundary = f"----local-music-engine-{secrets.token_hex(16)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                rendered.encode("utf-8"),
                b"\r\n",
            ]
        )
    safe_name = source_audio.name.replace('"', "")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="src_audio"; filename="{safe_name}"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode(),
            source_audio.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)
