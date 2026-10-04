"""Managed API authentication and route boundary, without models or inference."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from local_music_engine.ace_auth import ensure_api_key, read_api_key
from local_music_engine.ace_security import ALLOWED_ROUTES, MusicApiGuard


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MUSIC_ENGINE_ACE_API_KEY", raising=False)
    monkeypatch.delenv("ACESTEP_API_KEY", raising=False)
    destination = tmp_path / "runtime" / "ace-api-key"
    monkeypatch.setenv("MUSIC_ENGINE_ACE_API_KEY_FILE", str(destination))
    return destination


def test_concurrent_starters_share_one_complete_private_key(key_file):
    assert read_api_key() is None
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: ensure_api_key(), range(16)))
    assert len(set(keys)) == 1
    assert len(keys[0]) == 64
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert list(key_file.parent.iterdir()) == [key_file]
    assert read_api_key() == keys[0]


def test_credential_file_and_explicit_environment_are_shared(key_file, monkeypatch):
    key = ensure_api_key()
    assert read_api_key() == key
    monkeypatch.setenv("MUSIC_ENGINE_ACE_API_KEY", "configured-local-key")
    assert ensure_api_key() == "configured-local-key"
    assert key_file.read_text().strip() == key


def test_unsafe_credential_file_is_rejected_without_changing_it(key_file):
    key_file.parent.mkdir()
    key_file.write_text("private-key")
    key_file.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        ensure_api_key()
    assert key_file.read_text() == "private-key"
    key_file.unlink()
    outside = key_file.parent / "outside"
    outside.write_text("private-key")
    outside.chmod(0o600)
    key_file.symlink_to(outside)
    with pytest.raises(OSError):
        ensure_api_key()
    assert outside.read_text() == "private-key"


def request(method, path, *, headers=()):
    messages = []
    delegated = []

    async def upstream(scope, receive, send):
        delegated.append((scope["method"], scope["path"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    asyncio.run(MusicApiGuard(upstream, "private-key")(
        {"type": "http", "method": method, "path": path, "headers": list(headers)}, receive, send,
    ))
    return messages, delegated


@pytest.mark.parametrize("headers", [(), ((b"authorization", b"Bearer wrong-key"),),
    ((b"authorization", b"Bearer private-key"), (b"authorization", b"Bearer private-key"))])
def test_requests_without_one_correct_credential_do_not_reach_upstream(headers):
    messages, delegated = request("GET", "/health", headers=headers)
    assert messages[0]["status"] == 401
    assert delegated == []
    assert b"private-key" not in messages[1]["body"]


@pytest.mark.parametrize("origin", [b"null", b"http://localhost:9999", b"https://example.com"])
def test_browser_origins_are_denied_even_with_a_valid_credential(origin):
    messages, delegated = request("POST", "/release_task", headers=[
        (b"authorization", b"Bearer private-key"), (b"origin", origin),
    ])
    assert messages[0]["status"] == 403
    assert delegated == []
    assert all(name != b"access-control-allow-origin" for name, _ in messages[0]["headers"])


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/training/export"), ("POST", "/v1/dataset/save"),
    ("GET", "/docs"), ("GET", "/openapi.json"), ("OPTIONS", "/release_task"),
])
def test_unused_routes_are_disabled_before_the_upstream_handler(method, path):
    messages, delegated = request(method, path, headers=[(b"authorization", b"Bearer private-key")])
    assert messages[0]["status"] == 404
    assert delegated == []


@pytest.mark.parametrize("method,path", sorted(ALLOWED_ROUTES))
def test_authenticated_engine_routes_remain_usable(method, path):
    messages, delegated = request(method, path, headers=[(b"authorization", b"Bearer private-key")])
    assert messages[0]["status"] == 200
    assert delegated == [(method, path)]


def test_adapter_uses_the_shared_private_credential(key_file, monkeypatch):
    from local_music_engine.ace_adapter import AceStepClient
    key = ensure_api_key()
    client = AceStepClient()
    assert client.api_key == key
    assert AceStepClient(api_key="explicit-client-key").api_key == "explicit-client-key"


def test_readiness_client_discovers_a_key_published_after_its_construction(key_file, monkeypatch):
    from io import BytesIO
    from local_music_engine import ace_adapter
    client = ace_adapter.AceStepClient()
    assert client.api_key is None
    key = ensure_api_key()
    captured = []

    def local(request, timeout):
        captured.append(request.get_header("Authorization"))
        return BytesIO(b'{"data":{"status":"ok"}}')

    monkeypatch.setattr(ace_adapter, "open_local", local)
    assert client.health()["status"] == "ok"
    assert captured == ["Bearer " + key]
