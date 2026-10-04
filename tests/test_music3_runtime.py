"""Music 3 boundary/queue tests with an explicit fake backend, never real inference."""

from __future__ import annotations

import importlib.util
import json
import stat
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
import wave
from pathlib import Path

import pytest


@pytest.fixture
def runtime():
    path = Path(__file__).resolve().parents[1] / "scripts" / "music3_api_server.py"
    spec = importlib.util.spec_from_file_location("music3_runtime_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeEngine:
    real_inference = False

    def __init__(self, model_root):
        pass

    def generate(self, payload, destination, on_stage):
        on_stage("fake rendering", 0.5)
        with wave.open(str(destination), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(44_100)
            handle.writeframes(b"\x00\x01" * 2 * 882)
        return {"processingSeconds": 0.01, "peakMemoryGb": 0, "testOnly": True}


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for fake runtime worker")


@pytest.fixture
def server(runtime, tmp_path):
    state = runtime.Music3State(tmp_path / "runtime", tmp_path / "model", FakeEngine)
    state.start()
    wait_until(lambda: state.ready)
    http_server = runtime.Music3HttpServer(("127.0.0.1", 0), state, "private-test-key")
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    yield http_server
    http_server.shutdown()
    http_server.server_close()
    state.pending.put(None)
    state.worker.join(timeout=3)
    thread.join(timeout=3)


def request(server, method, path, payload=None, *, auth=True, origin=False):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer private-test-key"
    if origin:
        headers["Origin"] = "http://localhost"
    body = json.dumps(payload).encode() if payload is not None else None
    call = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(call, timeout=3) as response:
            content = response.read()
            return response.status, json.loads(content) if response.headers.get_content_type() == "application/json" else content
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def song(runtime, **changes):
    return {"prompt": "Warm emotional hip hop, 84 BPM", "lyrics": "[verse]\nCity lights are fading\n[chorus]\nStay until the morning",
            "audio_duration": 30, "seed": 7, "model": runtime.MODEL_ID, "batch_size": 1, "inference_steps": 30, **changes}


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/health", None), ("POST", "/release_task", {}),
    ("POST", "/query_result", {"task_id_list": []}), ("GET", "/v1/audio?artifact_id=abcd", None),
])
def test_every_route_requires_auth(server, method, path, payload):
    assert request(server, method, path, payload, auth=False)[0] == 401


def test_health_declares_actual_engine_and_fake_test_status(server, runtime):
    status, response = request(server, "GET", "/health")
    data = response["data"]
    assert status == 200 and data["models_initialized"]
    assert data["engine"] == "minimax-music3" and data["loaded_model"] == runtime.MODEL_ID
    assert data["loaded_lm_model"] is None and data["model_revision"] == runtime.MODEL_REVISION
    assert data["realInference"] is False
    assert data["capabilities"] == {"text2music": True, "cover": False, "repaint": False, "referenceAudio": False}
    assert data["max_duration_seconds"] == 300


def test_health_available_during_load_and_submission_fails(runtime, tmp_path):
    state = runtime.Music3State(tmp_path / "runtime", tmp_path / "model", FakeEngine)
    assert state.health()["models_initialized"] is False
    assert state.health()["realInference"] is False
    assert state.health()["loaded_model"] == runtime.MODEL_ID
    with pytest.raises(RuntimeError, match="still loading"):
        state.submit(song(runtime))


@pytest.fixture
def checkpoint_layout(runtime, tmp_path):
    """Metadata only; no weights or real inference are used in these checks."""
    root = tmp_path / "checkpoint"
    (root / "tokenizer").mkdir(parents=True)
    metadata = {
        "runtime-manifest.json": {
            "repository": runtime.MODEL_ID, "revision": runtime.MODEL_REVISION,
            "upstreamCommit": runtime.UPSTREAM_COMMIT, "quantization": "mxfp8",
        },
        "config.json": {
            **runtime.CHECKPOINT_CONFIG,
            "quantization": {"mode": "mxfp8", "group_size": 32, "bits": 8},
        },
        "tokenizer/tokenizer_config.json": {"tokenizer_class": "Qwen2Tokenizer"},
        "tokenizer/tokenizer.json": {
            "added_tokens": [{"content": token, "id": value}
                             for token, value in runtime.CHECKPOINT_SPECIAL_TOKENS.items()],
        },
    }
    for name, value in metadata.items():
        (root / name).write_text(json.dumps(value), encoding="utf-8")
    return root


def test_real_runtime_rejects_missing_tokenizer_before_model_import(runtime, checkpoint_layout):
    import shutil
    shutil.rmtree(checkpoint_layout / "tokenizer")
    # The ordinary project test environment does not have MLX. A missing real
    # tokenizer must fail explicitly before importing it, rather than reaching
    # the upstream tiny synthetic encoder and producing misleading audio.
    with pytest.raises(RuntimeError, match="tokenizer.json"):
        runtime.MlxMusic3Engine(checkpoint_layout)


def test_checkpoint_metadata_requires_caption_and_lyrics_token_ids(runtime, checkpoint_layout):
    runtime.validate_checkpoint_layout(checkpoint_layout)
    path = checkpoint_layout / "tokenizer" / "tokenizer.json"
    metadata = json.loads(path.read_text())
    metadata["added_tokens"][-1]["id"] = 42
    path.write_text(json.dumps(metadata))
    with pytest.raises(RuntimeError, match="special-token IDs"):
        runtime.validate_checkpoint_layout(checkpoint_layout)


@pytest.mark.parametrize("changes,error", [
    ({"sample_rate": 32_000}, "architecture/audio"),
    ({"audio_code_offset": 42}, "architecture/audio"),
    ({"quantization": {"mode": "mxfp4", "group_size": 32, "bits": 4}}, "MXFP8"),
])
def test_checkpoint_cannot_misdeclare_audio_or_quantization(runtime, checkpoint_layout, changes, error):
    path = checkpoint_layout / "config.json"
    metadata = json.loads(path.read_text())
    path.write_text(json.dumps({**metadata, **changes}))
    with pytest.raises(RuntimeError, match=error):
        runtime.validate_checkpoint_layout(checkpoint_layout)


def test_browser_and_unavailable_routes_are_rejected(server):
    assert request(server, "GET", "/health", origin=True)[0] == 403
    assert request(server, "GET", "/v1/audio?path=/etc/passwd")[0] == 400
    assert request(server, "POST", "/run_command", {"command": "echo danger"})[0] == 404
    assert request(server, "POST", "/v1/create_sample", {})[0] == 404


@pytest.mark.parametrize("changes", [
    {"task_type": "cover"}, {"model": "other-model"}, {"batch_size": 2}, {"batch_size": True},
    {"seed": -1}, {"seed": True}, {"seed": 2**32}, {"audio_duration": 0}, {"audio_duration": 301},
    {"audio_duration": float("nan")}, {"audio_duration": True}, {"inference_steps": 31},
    {"inference_steps": 2.5}, {"inference_steps": False}, {"lyrics": ""}, {"prompt": ""},
    {"reference_audio_path": "/private/input.wav"}, {"src_audio": "data"}, {"command": "echo danger"},
])
def test_generation_contract_is_strict(runtime, changes):
    with pytest.raises(ValueError):
        runtime.validate_request(song(runtime, **changes))


@pytest.mark.parametrize("field,limit", [("prompt", 16_000), ("lyrics", 32_000)])
def test_text_limits_apply_to_full_input_before_whitespace_validation(runtime, field, limit):
    # Trimming would make this short and valid, but the complete submitted text
    # must remain inside the API bound rather than being silently shortened.
    value = " " * limit + "[instrumental]"
    with pytest.raises(ValueError, match="characters"):
        runtime.validate_request(song(runtime, **{field: value}))


def test_server_boundary_preserves_frozen_prompt_and_lyric_whitespace(server, runtime):
    prompt = " \nWarm emotional hip hop, 84 BPM.\n "
    lyrics = "\n[verse]\nCity lights are fading\n\n[chorus]\nStay with me\n "
    status, submitted = request(server, "POST", "/release_task", song(runtime, prompt=prompt, lyrics=lyrics))
    assert status == 200
    task_id = submitted["data"]["task_id"]
    wait_until(lambda: server.state.tasks[task_id]["status"] == 1)
    stored = server.state.tasks[task_id]["request"]
    assert stored["prompt"] == prompt and stored["lyrics"] == lyrics


def test_async_completion_download_and_artifact_integrity(server, runtime):
    status, submitted = request(server, "POST", "/release_task", song(runtime))
    assert status == 200
    task_id = submitted["data"]["task_id"]
    wait_until(lambda: server.state.tasks[task_id]["status"] == 1)
    status, queried = request(server, "POST", "/query_result", {"task_id_list": [task_id]})
    row = queried["data"][0]
    result = json.loads(row["result"])[0]
    assert status == 200 and row["status"] == 1 and result["progress"] == 1.0
    assert result["seed_value"] == 7 and result["metas"]["requestedDurationSeconds"] == 30
    assert result["metas"]["duration"] == 0.02  # actual duration, never the request's duration
    status, content = request(server, "GET", result["file"])
    assert status == 200 and content[:4] == b"RIFF"
    artifact = server.state.output_root / f"{task_id}.wav"
    artifact.write_bytes(content[:-2] + b"xx")  # same length but a different SHA-256
    assert request(server, "GET", result["file"])[0] == 404


def test_new_requests_always_create_new_tasks(server, runtime):
    first = request(server, "POST", "/release_task", song(runtime))[1]["data"]["task_id"]
    second = request(server, "POST", "/release_task", song(runtime))[1]["data"]["task_id"]
    assert first != second


def test_unknown_and_invalid_task_ids(server):
    assert request(server, "POST", "/query_result", {"task_id_list": ["a" * 32]})[0] == 404
    assert request(server, "POST", "/query_result", {"task_id_list": ["../../file"]})[0] == 400
    assert request(server, "GET", "/v1/audio?artifact_id=" + "a" * 32)[0] == 404


def test_interrupted_tasks_fail_after_restart(runtime, tmp_path):
    root = tmp_path / "runtime"
    state = runtime.Music3State(root, tmp_path / "model", FakeEngine)
    state.ready = True
    task_id = state.submit(song(runtime))
    restored = runtime.Music3State(root, tmp_path / "model", FakeEngine)
    assert restored.tasks[task_id]["status"] == 2
    assert "interrupted" in restored.tasks[task_id]["stage"]


def test_generation_exception_is_a_failed_task(runtime, tmp_path):
    class FailingEngine(FakeEngine):
        def generate(self, payload, destination, on_stage):
            raise RuntimeError("explicit fake failure")
    state = runtime.Music3State(tmp_path / "runtime", tmp_path / "model", FailingEngine)
    state.start()
    wait_until(lambda: state.ready)
    task_id = state.submit(song(runtime))
    wait_until(lambda: state.tasks[task_id]["status"] == 2)
    result = json.loads(state.query([task_id])[0]["result"])[0]
    assert "explicit fake failure" in result["error"] and "file" not in result
    state.pending.put(None)
    state.worker.join(timeout=3)


def test_private_key_creation_reuse_and_permissions(runtime, tmp_path, monkeypatch):
    path = tmp_path / "music3-api-key"
    monkeypatch.setenv("MUSIC_ENGINE_MUSIC3_API_KEY_FILE", str(path))
    monkeypatch.delenv("MUSIC_ENGINE_MUSIC3_API_KEY", raising=False)
    first = runtime.ensure_api_key()
    assert len(first) == 64 and runtime.ensure_api_key() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        runtime.ensure_api_key()


def test_key_symlink_is_rejected(runtime, tmp_path, monkeypatch):
    private = tmp_path / "private-key"
    private.write_text("private-secret")
    private.chmod(0o600)
    link = tmp_path / "music3-api-key"
    link.symlink_to(private)
    monkeypatch.setenv("MUSIC_ENGINE_MUSIC3_API_KEY_FILE", str(link))
    monkeypatch.delenv("MUSIC_ENGINE_MUSIC3_API_KEY", raising=False)
    with pytest.raises(OSError):
        runtime.ensure_api_key()


@pytest.mark.parametrize("lyrics", ["[Verse] City lights", " [chorus]한글 가사", "[verse][chorus]\nWords"])
def test_direct_api_rejects_inline_section_text(runtime, lyrics):
    with pytest.raises(ValueError, match="own line"):
        runtime.validate_request(song(runtime, lyrics=lyrics))


def test_runtime_owner_blocks_second_process_before_registry_changes(runtime, tmp_path):
    import subprocess
    root = tmp_path / "runtime"
    registry = root / "tasks.json"
    root.mkdir()
    original = b'{"keep": "unfinished task must not be repaired by a duplicate"}'
    registry.write_bytes(original)
    module_path = Path(runtime.__file__)
    child = """import importlib.util,sys
spec=importlib.util.spec_from_file_location('music3_runtime_child',sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
try:
    with module.RuntimeOwner(module.Path(sys.argv[2])):
        print('unexpected owner')
except RuntimeError as error:
    print(str(error))
"""
    with runtime.RuntimeOwner(root):
        import sys
        process = subprocess.run([sys.executable, "-c", child, str(module_path), str(root)], capture_output=True, text=True, timeout=3)
        assert process.returncode == 0 and "already has a running server" in process.stdout
        assert registry.read_bytes() == original
    with runtime.RuntimeOwner(root):
        pass  # The owner is released when the server process exits.


def test_eager_ar_preserves_seed_frames_and_materializes_cache(runtime):
    class Hidden:
        def __getitem__(self, key):
            return 0.0
    class Random:
        def seed(self, value):
            self.seed_value = value
        def key(self, value):
            return value
        def split(self, value):
            return value + 1, value + 100
    class FakeMx:
        def __init__(self):
            self.random = Random()
            self.evaluated = []
        def eval(self, *values):
            self.evaluated.append(values)
        def stack(self, frames, axis):
            assert axis == 1
            return tuple(frames)
    def run(eager):
        mx = FakeMx()
        calls = []
        caches = []
        class Ar:
            def qwen3_hidden(self, model, embeddings):
                return Hidden(), [SimpleNamespace(state=(0.0, 0.0))]
            def ar_one_frame(self, model, depth, config, last_hidden, cache, key, emit_frame):
                index = len(calls)
                calls.append((key, emit_frame))
                state = (float(index), float(index + 1))
                caches.append(state)
                return SimpleNamespace(last_hidden=float(index + 1), cache=[SimpleNamespace(state=state)],
                                       ended=False, frame_hidden=last_hidden + key * 0.001)
        ar = Ar()
        language = SimpleNamespace(model=SimpleNamespace(embed_tokens=lambda value: value))
        events = []
        if eager:
            frames = runtime.generate_frames_eager(mx, ar, language, None, None, [1, 2], 3, 7,
                lambda count, maximum: events.append((count, maximum)))
        else:
            # Reference evaluation order from the pinned AR sampler, without
            # materialization boundaries. No MLX/model inference occurs here.
            mx.random.seed(7)
            key = mx.random.key(7)
            hidden, cache = ar.qwen3_hidden(language, [1, 2])
            last = hidden[:, -1]
            frames = []
            for index in range(4):
                key, subkey = mx.random.split(key)
                result = ar.ar_one_frame(language, None, None, last, cache, subkey, emit_frame=index > 0)
                last, cache = result.last_hidden, result.cache
                if index > 0:
                    frames.append(result.frame_hidden)
            frames = tuple(frames)
        return frames, calls, mx, caches, events
    actual, actual_calls, mx, caches, events = run(True)
    expected, expected_calls, _, _, _ = run(False)
    assert actual == expected and actual_calls == expected_calls
    assert mx.random.seed_value == 7 and len(actual) == 3
    assert len(mx.evaluated) == 5  # every frame, then the completed stack
    assert [evaluation[2][0] for evaluation in mx.evaluated[:-1]] == caches
    assert events == [(3, 3)]
