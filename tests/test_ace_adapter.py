import http.client
import io
import json
import urllib.error
from typing import Any

import pytest

from local_music_engine import ace_adapter
from local_music_engine.ace_adapter import (
    POLL_RETRIES, AceApiError, AceServerError, AceStepClient, AceUnavailable, stops_submissions,
)

URL = "http://127.0.0.1:18001"
DONE = json.dumps({"data": [{"status": 1, "result": json.dumps([{"file": "/v1/audio?path=x.wav"}])}]}).encode()
DROPPED = [http.client.RemoteDisconnected("Remote end closed connection without response"),
           ConnectionResetError(54, "Connection reset by peer"), TimeoutError("timed out"),
           http.client.IncompleteRead(b'{"data"'), http.client.BadStatusLine("")]


def serve(monkeypatch: pytest.MonkeyPatch, answers: list[Any]) -> list[str]:
    """Loopback answers in order: an exception is raised, bytes are the body."""

    calls: list[str] = []

    def open_local(request: Any, *, timeout: float) -> Any:
        item = answers[len(calls)]
        calls.append(request.full_url)
        if isinstance(item, BaseException):
            raise item
        return io.BytesIO(item)

    monkeypatch.setattr(ace_adapter, "open_local", open_local)
    return calls


@pytest.mark.parametrize("dropped", DROPPED, ids=lambda error: type(error).__name__)
def test_a_connection_that_breaks_before_the_answer_is_an_engine_error(dropped: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, [dropped])
    with pytest.raises(AceUnavailable, match=f"ACE API unavailable: {type(dropped).__name__}") as caught:
        AceStepClient(URL, api_key="").submit({"seed": 1})
    assert stops_submissions(caught.value)


def test_an_http_refusal_is_an_engine_error(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, [urllib.error.HTTPError(f"{URL}/release_task", 503, "busy", None, io.BytesIO(b"queue full"))])
    with pytest.raises(AceServerError, match="HTTP 503: queue full"):
        AceStepClient(URL, api_key="").submit({"seed": 1})


def test_a_status_call_dropped_while_rendering_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = serve(monkeypatch, [*DROPPED[:POLL_RETRIES], DONE])
    assert AceStepClient(URL, api_key="").wait("task", poll_seconds=0) == {"file": "/v1/audio?path=x.wav"}
    assert len(calls) == POLL_RETRIES + 1


def test_a_server_that_stays_gone_ends_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = serve(monkeypatch, [ConnectionResetError(54, "reset")] * (POLL_RETRIES + 1))
    with pytest.raises(AceUnavailable):
        AceStepClient(URL, api_key="").wait("task", poll_seconds=0)
    assert len(calls) == POLL_RETRIES + 1


def test_only_errors_about_the_server_stop_further_seeds() -> None:
    assert stops_submissions(TimeoutError("ACE task timed out: task"))
    assert stops_submissions(AceUnavailable("ACE API unavailable: RemoteDisconnected"))
    assert stops_submissions(AceServerError("ACE engine did not start (exit 1)"))
    # A task the server finished with an error leaves it free; the wording does not matter.
    assert not stops_submissions(AceApiError("task failed: API unavailable for this prompt"))
    assert not stops_submissions(RuntimeError("API HTTP 500"))
