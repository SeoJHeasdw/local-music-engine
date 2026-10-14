import threading
import time
from pathlib import Path
from typing import Any

import pytest

from local_music_engine import ace_runtime, cli
from local_music_engine.ace_adapter import AceApiError, AceUnavailable
from local_music_engine.ace_runtime import ManagedAceClient, ensure_ace, start_lock, stop_when_idle
from local_music_engine.engines import ACE_DIT_MODEL
from local_music_engine.lane import shared_lane
from local_music_engine.storage import ProjectStore
from local_music_engine.workflow import DEFAULT_LM_MODEL

URL = "http://127.0.0.1:18001"


class FakeProcess:
    def __init__(self, code: int | None = None):
        self.returncode = code

    def poll(self) -> int | None:
        return self.returncode


def answers(monkeypatch: pytest.MonkeyPatch, sequence: list[Any]) -> list[Any]:
    """Health answers in order; an exception is raised, the last answer repeats."""

    seen: list[Any] = []

    def probe(_client):
        item = sequence[min(len(seen), len(sequence) - 1)]
        seen.append(item)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ace_runtime.AceStepClient, "health", probe)
    return seen


def test_a_run_starts_ace_when_nothing_answers_and_waits_for_its_models(monkeypatch: pytest.MonkeyPatch) -> None:
    answers(monkeypatch, [AceUnavailable("off"), AceUnavailable("loading"), {"status": "ok", "models_initialized": False},
                          {"status": "ok", "models_initialized": True}])
    started = []

    def spawn(base_url: str, **models: str) -> FakeProcess:
        started.append((base_url, models))
        return FakeProcess()

    ensure_ace(URL, model=ACE_DIT_MODEL, lm_model="lm", poll=0, spawn=spawn)
    assert started == [(URL, {"model": ACE_DIT_MODEL, "lm_model": "lm"})]


def test_a_running_server_is_used_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    answers(monkeypatch, [{"status": "ok", "models_initialized": True}])
    ensure_ace(URL, model=ACE_DIT_MODEL, lm_model="lm", poll=0,
               spawn=lambda *_args, **_kwargs: pytest.fail("a server that answers must not be started again"))


def test_a_server_that_exits_or_fails_to_load_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    answers(monkeypatch, [AceUnavailable("off")])
    with pytest.raises(AceApiError, match="did not start"):
        ensure_ace(URL, model=ACE_DIT_MODEL, lm_model="lm", poll=0, spawn=lambda *_a, **_k: FakeProcess(1))
    answers(monkeypatch, [{"status": "ok", "stage": "failed"}])
    with pytest.raises(AceApiError, match="failed to load"):
        ensure_ace(URL, model=ACE_DIT_MODEL, lm_model="lm", poll=0)
    answers(monkeypatch, [AceUnavailable("off")])
    with pytest.raises(AceApiError, match="did not finish loading"):
        ensure_ace(URL, model=ACE_DIT_MODEL, lm_model="lm", poll=0, timeout=0, spawn=lambda *_a, **_k: FakeProcess())


def test_the_managed_client_brings_ace_up_once_before_its_first_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(ace_runtime, "ensure_ace", lambda base_url, **models: calls.append((base_url, models)))
    monkeypatch.setattr(ace_runtime.AceStepClient, "_request", lambda self, *_a, **_k: b'{"data": {"status": "ok"}}')
    client = ManagedAceClient(URL, model="dit", lm_model="lm")
    assert calls == []  # constructing a client starts nothing
    client.health()
    client.health()
    assert calls == [(URL, {"model": "dit", "lm_model": "lm"})]


def test_ace_stops_itself_only_once_the_lane_is_free() -> None:
    stopped = threading.Event()
    with shared_lane():
        stop_when_idle(poll=0.05, idle_polls=2, stop=stopped.set)
        assert not stopped.wait(0.4)
    assert stopped.wait(2.0)


def test_a_run_that_takes_the_lane_while_ace_checks_keeps_it() -> None:
    stopped = threading.Event()
    with start_lock():  # the watcher waits here, as it would behind a starting run
        stop_when_idle(poll=0.05, idle_polls=2, stop=stopped.set)
        time.sleep(0.3)
        lane = shared_lane()
        lane.__enter__()
    try:
        assert not stopped.wait(0.4)
    finally:
        lane.__exit__(None, None, None)
    assert stopped.wait(2.0)


def _song(tmp_path: Path) -> Path:
    store = ProjectStore.initialize(tmp_path / "song", title="t", lyrics="[Instrumental]", style_prompt="piano",
                                    target_duration_seconds=10)
    return store.root


def test_cli_generation_commands_start_ace_with_the_models_they_need(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def capture(name: str):
        def call(*_args: Any, client_factory=None, **_kwargs: Any) -> dict[str, Any]:
            client = client_factory(URL)
            seen[name] = (type(client).__name__, client._engine_models)
            return {}
        return call

    for name in ("generate_candidates", "repaint_candidate", "cover_candidates", "resume_latest_batch"):
        monkeypatch.setattr(cli, name, capture(name))
    song = str(_song(tmp_path))
    cli.run(cli.build_parser().parse_args(["generate", song, "--seeds", "1", "--model", "acestep-v15-turbo"]))
    cli.run(cli.build_parser().parse_args(["repaint", song, "--start", "1", "--end", "2", "--seed", "3"]))
    cli.run(cli.build_parser().parse_args(["cover", song, "--candidate-id", "candidate_x", "--seeds", "4"]))
    cli.run(cli.build_parser().parse_args(["resume", song]))
    assert seen["generate_candidates"] == ("ManagedAceClient", ("acestep-v15-turbo", DEFAULT_LM_MODEL))
    # A version or batch the project cannot name falls back to the defaults; the run reports why.
    for name in ("repaint_candidate", "cover_candidates", "resume_latest_batch"):
        assert seen[name] == ("ManagedAceClient", (ACE_DIT_MODEL, DEFAULT_LM_MODEL))
