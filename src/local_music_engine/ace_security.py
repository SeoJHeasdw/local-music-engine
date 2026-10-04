"""Narrow authenticated ASGI boundary for the managed inference-only ACE server.

It wraps the pinned upstream app without changing that checkout. Browser requests
are unnecessary: only Electron main and the CLI communicate with this server.
"""

from __future__ import annotations

import hmac
import json
from typing import Any, Awaitable, Callable

from .ace_auth import validate_api_key

ALLOWED_ROUTES = frozenset({
    ("GET", "/health"),
    ("POST", "/release_task"),
    ("POST", "/query_result"),
    ("POST", "/v1/create_sample"),
    ("GET", "/v1/audio"),
})


class MusicApiGuard:
    def __init__(self, app: Callable[..., Awaitable[None]], api_key: str):
        self.app = app
        self.authorization = ("Bearer " + validate_api_key(api_key)).encode("ascii")

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        authorization = [value for name, value in headers if name.lower() == b"authorization"]
        if len(authorization) != 1 or not hmac.compare_digest(authorization[0], self.authorization):
            await self.reject(send, 401, "ACE API authentication required")
            return
        if any(name.lower() == b"origin" for name, _ in headers):
            await self.reject(send, 403, "Browser access is disabled")
            return
        if (scope["method"], scope["path"]) not in ALLOWED_ROUTES:
            await self.reject(send, 404, "API route is unavailable")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def reject(send: Callable, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
        ]})
        await send({"type": "http.response.body", "body": body})
