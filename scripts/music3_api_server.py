"""Authenticated loopback-only Music 3 worker, using the pinned native MLX runtime.

Only generation JSON and opaque, completed audio identifiers cross this boundary.
Model imports and inference run on one worker thread. This script deliberately
has no renderer/file-path API.

Precision follows the reference SGLang-Omni server: the global and local LMs run
in bfloat16, the condition encoder, DiT and vocoder in float32. Both stages do
not fit together on a 36 GB Mac, so each song loads the AR stage, generates its
frame hidden states, releases it, then loads and runs the acoustic stage.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import hmac
import json
import math
import os
import queue
import re
import resource
import secrets
import socket
import stat
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PROJECT_ROOT / ".runtime" / "minimax-music3"
MODEL_ROOT = PROJECT_ROOT / ".runtime" / "models" / "minimax-music3-bf16"
MODEL_ID = "mlx-community/MiniMax-Music3-bf16"
MODEL_REVISION = "83a5f2d365673689df5c8f36e21e108751fd92ea"
# SHA-256 of the pinned revision's weight shards (HF LFS); bootstrap verifies them.
MODEL_SHARDS = {
    "model-00001-of-00006.safetensors": (5_295_999_536, "4dd1cf1ad7655c464384b04974cac314cb90f6ac0d0c2dd3e43b34981cc7f386"),
    "model-00002-of-00006.safetensors": (5_301_856_783, "3b915b7e58b5a3ece522b9a7faf8a49ed6713bc4e06665b500c4968cc97dc772"),
    "model-00003-of-00006.safetensors": (4_932_746_826, "b830d6d901b5d7b1c25f6e7408e85d905938a7feab94dbfa2cf00d6374b68039"),
    "model-00004-of-00006.safetensors": (5_305_618_097, "f6d5ac9485452af285a61b87d2ec3c1105bdfa300390d1ac94c51fe176b9a3c2"),
    "model-00005-of-00006.safetensors": (5_354_074_472, "138fc0f93deb3a1b18386b737a9cad44ba5bf299835f622951c3a8c30d1f649e"),
    "model-00006-of-00006.safetensors": (2_315_753_430, "5e9da4b2442d52f4f26ec39890a0d301210ccce3eebbc06a4da92ae7628951c7"),
}
AR_DTYPE = "bfloat16"
ACOUSTIC_DTYPES = ("float32", "bfloat16")
UPSTREAM_COMMIT = "784b29e2691a93ca7483147d86f61859dfaa6296"
MAX_DURATION = 300
MAX_BODY_BYTES = 262_144
CAPABILITIES = {"text2music": True, "cover": False, "repaint": False, "referenceAudio": False}
MEMORY_POLICY = "staged-ar-then-acoustic-v2"
CHECKPOINT_CONFIG = {
    "model_type": "minimax_music3", "hidden_size": 4096, "num_hidden_layers": 36,
    "num_codebooks": 8, "audio_code_offset": 151675, "audio_cfg_token_id": 151654,
    "audio_end_token_id": 151670, "frame_rate": 25.0, "sample_rate": 44_100,
    "output_sampling_rate": 44_100, "output_hop_length": 512,
}
CHECKPOINT_SPECIAL_TOKENS = {
    "<|audio_cfg|>": 151654, "<|audio_start|>": 151669, "<|audio_end|>": 151670,
    "<|caption_start|>": 151671, "<|caption_end|>": 151672,
    "<|lyrics_start|>": 151673, "<|lyrics_end|>": 151674,
}


def validate_checkpoint_layout(model_root: Path) -> None:
    """Require the actual checkpoint tokenizer, never upstream's tiny fallback.

    Strict weight loading alone does not check the caption/lyrics tokenizer.
    The upstream model intentionally has a synthetic text encoder for tiny
    architecture tests when its tokenizer directory is absent. That fallback
    must never be reachable from this real-inference server.
    """
    def read_object(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Music 3 checkpoint asset is missing or invalid: {path.name}") from error
        if not isinstance(value, dict):
            raise RuntimeError(f"Music 3 checkpoint asset is not an object: {path.name}")
        return value

    manifest = read_object(model_root / "runtime-manifest.json")
    if (manifest.get("revision") != MODEL_REVISION or manifest.get("upstreamCommit") != UPSTREAM_COMMIT
            or manifest.get("repository") != MODEL_ID or manifest.get("precision") != "bf16"
            or manifest.get("shards") != {name: {"bytes": size, "sha256": digest} for name, (size, digest) in MODEL_SHARDS.items()}):
        raise RuntimeError("Music 3 model/runtime manifest differs from the pinned bootstrap")
    for name, (size, _) in MODEL_SHARDS.items():
        shard = model_root / name
        if shard.is_symlink() or not shard.is_file() or shard.stat().st_size != size:
            raise RuntimeError(f"Music 3 weight shard is missing or changed: {name}")
    config = read_object(model_root / "config.json")
    if any(config.get(key) != value for key, value in CHECKPOINT_CONFIG.items()):
        raise RuntimeError("Music 3 checkpoint architecture/audio configuration differs from the pinned model")
    if config.get("quantization") or config.get("quantization_config") or config.get("torch_dtype") != "bfloat16":
        raise RuntimeError("Music 3 checkpoint must be the pinned dense BF16 conversion")
    tokenizer = read_object(model_root / "tokenizer" / "tokenizer.json")
    tokenizer_config = read_object(model_root / "tokenizer" / "tokenizer_config.json")
    if tokenizer_config.get("tokenizer_class") != "Qwen2Tokenizer":
        raise RuntimeError("Music 3 checkpoint tokenizer class differs from the pinned model")
    added_tokens = tokenizer.get("added_tokens")
    if not isinstance(added_tokens, list):
        raise RuntimeError("Music 3 checkpoint tokenizer special tokens are missing")
    tokens = {entry.get("content"): entry.get("id") for entry in added_tokens if isinstance(entry, dict)}
    if any(tokens.get(token) != value for token, value in CHECKPOINT_SPECIAL_TOKENS.items()):
        raise RuntimeError("Music 3 checkpoint tokenizer special-token IDs differ from the pinned model")


class RuntimeOwner:
    """Hold one process-wide runtime owner before reading or repairing tasks."""

    def __init__(self, runtime_root: Path):
        self.path = runtime_root / "server.lock"
        self.descriptor: int | None = None

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise RuntimeError("Music 3 runtime owner file must be private and owned by this user")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Music 3 runtime already has a running server") from error
            os.ftruncate(descriptor, 0)
            os.write(descriptor, (str(os.getpid()) + "\n").encode("ascii"))
            os.fsync(descriptor)
            self.descriptor = descriptor
            return self
        except BaseException:
            os.close(descriptor)
            raise

    def __exit__(self, *args):
        if self.descriptor is not None:
            import fcntl
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None


def generate_frames_eager(mx, ar, language_model, depth, config, text_ids, max_frames, seed, on_frame):
    """Same pinned AR algorithm, materializing each frame and its KV cache.

    Upstream keeps every frame_hidden lazy until all frames have been sampled.
    Those branches can retain intermediate depth-decoder graphs for the entire
    song. Evaluation boundaries change memory lifetime, not sampling or math.
    """
    mx.random.seed(seed)
    key = mx.random.key(seed)
    embeddings = language_model.model.embed_tokens(text_ids)
    hidden, cache = ar.qwen3_hidden(language_model, embeddings)
    last_hidden = hidden[:, -1]
    frames = []
    for frame_index in range(max_frames + 1):
        key, subkey = mx.random.split(key)
        result = ar.ar_one_frame(language_model, depth, config, last_hidden, cache, subkey, emit_frame=frame_index > 0)
        last_hidden, cache = result.last_hidden, result.cache
        if result.ended:
            break
        # Cut graph references before retaining the next frame. Materialize the
        # cache as well as the output so no lazy cache branch survives a step.
        mx.eval(result.frame_hidden, last_hidden, [entry.state for entry in cache])
        if frame_index > 0:
            frames.append(result.frame_hidden)
            if len(frames) % 25 == 0 or len(frames) == max_frames:
                on_frame(len(frames), max_frames)
            if len(frames) >= max_frames:
                break
    if not frames:
        raise ValueError("MiniMax Music 3 generated zero audio frames")
    output = mx.stack(frames, axis=1)
    mx.eval(output)
    return output


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_key(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("Music 3 API credential is empty or invalid")
    return value


def ensure_api_key() -> str:
    explicit = os.environ.get("MUSIC_ENGINE_MUSIC3_API_KEY")
    if explicit is not None:
        return _validate_key(explicit)
    path = Path(os.environ.get("MUSIC_ENGINE_MUSIC3_API_KEY_FILE", str(PROJECT_ROOT / ".runtime" / "music3-api-key"))).expanduser().absolute()

    def read() -> str | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "r", encoding="ascii") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
                raise ValueError("Music 3 credential file must be private and owned by this user")
            return _validate_key(handle.read(4097))

    existing = read()
    if existing is not None:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".music3-api-key-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(32) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    key = read()
    if key is None:
        raise RuntimeError("Music 3 credential could not be published")
    return key


def validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Generation request must be a JSON object")
    allowed = {"prompt", "lyrics", "audio_duration", "seed", "model", "batch_size", "inference_steps", "task_type"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("Unsupported Music 3 request fields: " + ", ".join(sorted(unknown)))
    if payload.get("task_type", "text2music") != "text2music":
        raise ValueError("Music 3 supports text2music only")
    if payload.get("model", MODEL_ID) != MODEL_ID:
        raise ValueError("The requested model differs from this server's pinned Music 3 checkpoint")
    prompt, lyrics = payload.get("prompt"), payload.get("lyrics")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16_000:
        raise ValueError("A music description of at most 16000 characters is required")
    if not isinstance(lyrics, str) or not lyrics.strip() or len(lyrics) > 32_000:
        raise ValueError("Structured lyrics of at most 32000 characters are required; use [instrumental] for instrumental music")
    if any(re.match(r"^[ \t]*\[[^\]]+\][ \t]*\S", line) for line in lyrics.splitlines()):
        raise ValueError("Each lyric section tag must be on its own line; inline tag text would be lost by the checkpoint tokenizer")
    duration = payload.get("audio_duration", 60)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 1 <= duration <= MAX_DURATION:
        raise ValueError(f"audio_duration must be between 1 and {MAX_DURATION} seconds")
    seed = payload.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError("seed must be an integer between 0 and 4294967295")
    steps = payload.get("inference_steps", 30)
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 30:
        raise ValueError("inference_steps must be an integer between 1 and 30")
    batch = payload.get("batch_size", 1)
    if isinstance(batch, bool) or not isinstance(batch, int) or batch != 1:
        raise ValueError("Music 3 processes exactly one song per request")
    # Whitespace is part of the checkpoint's assembled prompt/tokenizer contract.
    # Validate emptiness above without changing the frozen text that was sent.
    return {"prompt": prompt, "lyrics": lyrics, "audio_duration": float(duration), "seed": seed,
            "model": MODEL_ID, "batch_size": 1, "inference_steps": steps, "task_type": "text2music"}


def audio_identity(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as handle:
        frames, sample_rate, channels = handle.getnframes(), handle.getframerate(), handle.getnchannels()
        if frames <= 0 or sample_rate != 44_100 or channels != 2:
            raise RuntimeError("Music 3 produced an invalid stereo 44.1 kHz WAV")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"bytes": path.stat().st_size, "sha256": digest,
            "durationSeconds": frames / sample_rate, "sampleRate": sample_rate, "channels": channels}


class MlxMusic3Engine:
    """Owns the checkpoint path; every song loads the AR stage, then the acoustic stage.

    Nothing stays resident between songs, which leaves memory for quality checks
    and the optional assistant. All methods run on the single generation worker.
    """

    real_inference = True

    def __init__(self, model_root: Path, acoustic_dtype: str = "float32"):
        if acoustic_dtype not in ACOUSTIC_DTYPES:
            raise ValueError(f"MUSIC_ENGINE_MUSIC3_ACOUSTIC_DTYPE must be one of {', '.join(ACOUSTIC_DTYPES)}")
        validate_checkpoint_layout(model_root)
        import mlx.core as mx

        self.mx = mx
        self.model_root = model_root
        self.precision = {"profile": f"official-ar-{AR_DTYPE}-acoustic-{acoustic_dtype}",
                          "ar": AR_DTYPE, "acoustic": acoustic_dtype,
                          "reference": "SGLang-Omni MiniMax Music 3 (AR bfloat16, DiT/vocoder float32)"}
        self.acoustic_dtype = {"float32": mx.float32, "bfloat16": mx.bfloat16}[acoustic_dtype]
        mx.set_cache_limit(512 * 1024 * 1024)
        # MLX's default limit is 1.5x the recommended working set (about 45 GB on
        # a 36 GB machine). Leave room for the app, OS and the QC process.
        info = mx.device_info()
        memory_limit = min(int(info["memory_size"] * 0.66), int(info["max_recommended_working_set_size"]))
        mx.set_memory_limit(memory_limit)
        mx.set_wired_limit(memory_limit)
        self.memory_limit_bytes = memory_limit
        self.memory_observer: Callable[[dict[str, Any]], None] | None = None
        self.last_memory = self._memory_snapshot("idle")

    def _memory_snapshot(self, stage: str) -> dict[str, Any]:
        snapshot = {"stage": stage, "activeGb": self.mx.get_active_memory() / 1e9,
                    "cacheGb": self.mx.get_cache_memory() / 1e9, "peakGb": self.mx.get_peak_memory() / 1e9,
                    "memoryLimitGb": self.memory_limit_bytes / 1e9,
                    "processPeakRssGb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9,
                    "observedAt": time.time()}
        self.last_memory = snapshot
        if self.memory_observer:
            self.memory_observer(snapshot)
        return snapshot

    def _release(self) -> None:
        gc.collect()
        self.mx.clear_cache()

    def generate(self, payload: dict[str, Any], destination: Path, on_stage: Callable[[str, float], None]) -> dict[str, Any]:
        try:
            return self._generate_audio(payload, destination, on_stage)
        finally:
            # Locals holding either stage are gone; return to an empty footprint.
            self._release()
            self._memory_snapshot("idle")

    def _generate_audio(self, payload: dict[str, Any], destination: Path, on_stage: Callable[[str, float], None]) -> dict[str, Any]:
        import mlx.nn as nn
        import numpy as np
        from mlx_audio.music import load
        from mlx_audio.music.models.minimax_music3 import ar
        from mlx_audio.music.models.minimax_music3 import minimax_music3 as upstream

        mx = self.mx
        mx.reset_peak_memory()
        started = time.monotonic()
        on_stage("loading song planner (BF16)", 0.02)
        model = load(self.model_root, lazy=True, strict=True)
        mx.eval(model.language_model.parameters(), model.rvq_depth_decoder.parameters())
        self._memory_snapshot("song planner loaded")

        def on_frame(count: int, maximum: int):
            stage = f"planning song and vocals ({count}/{maximum} frames)"
            on_stage(stage, 0.05 + 0.4 * count / maximum)
            self._memory_snapshot(stage)

        on_stage("planning song and vocals", 0.05)
        text_ids = model._text_ids(payload["prompt"], payload["lyrics"])
        max_frames = max(1, int(payload["audio_duration"] * model.config.frame_rate))
        frames = generate_frames_eager(mx, ar, model.language_model, model.rvq_depth_decoder, model.config,
                                       text_ids, max_frames, payload["seed"], on_frame)
        mx.eval(frames)
        frame_count = int(frames.shape[1])
        planner_seconds = time.monotonic() - started
        # Release the 8B planner before the acoustic stage is materialized.
        model.language_model, model.rvq_depth_decoder = nn.Module(), nn.Module()
        del text_ids
        self._release()
        on_stage("loading audio renderer", 0.47)
        for component in (model.condition_encoder, model.transformer, model.vocoder):
            component.set_dtype(self.acoustic_dtype)
        mx.eval(model.condition_encoder.parameters(), model.transformer.parameters(), model.vocoder.parameters())
        frames = frames.astype(self.acoustic_dtype)
        self._memory_snapshot("audio renderer loaded")

        on_stage("rendering audio", 0.5)
        starts = upstream._chunk_starts(frame_count)
        waves = []
        previous_latent = previous_condition = None
        mx.random.seed(payload["seed"] + 7)
        for index, start in enumerate(starts):
            end = min(start + upstream.CHUNK_FRAMES, frame_count)
            condition = model.condition_encoder(frames[:, start:end])
            mx.eval(condition)
            noise = mx.random.normal((1, model.config.dit_in_channels, condition.shape[1])).astype(condition.dtype)
            latents, condition = upstream.denoise_chunk(model.transformer, noise, condition,
                num_inference_steps=payload["inference_steps"], guidance_scale=upstream.DIT_CFG_SCALE,
                previous_latent=previous_latent, previous_condition=previous_condition)
            carry_start = max(0, latents.shape[-1] - 2 * upstream.OVERLAP_LATENT_LENGTH)
            carry_end = max(carry_start, latents.shape[-1] - upstream.OVERLAP_LATENT_LENGTH)
            previous_latent = latents[..., carry_start:carry_end]
            previous_condition = condition[:, carry_start:carry_end]
            cropped = upstream._crop_waveform(model.vocoder(latents), index, len(starts))
            mx.eval(cropped, previous_latent, previous_condition)
            waves.append(cropped)
            del condition, noise, latents
            mx.clear_cache()
            stage = f"rendering audio ({index + 1}/{len(starts)} chunks)"
            on_stage(stage, 0.5 + 0.4 * (index + 1) / len(starts))
            self._memory_snapshot(stage)
        audio = mx.concatenate(waves, axis=-1)
        waveform = np.asarray(mx.clip(audio[0].transpose(1, 0).astype(mx.float32), -1.0, 1.0))
        del model, frames, waves, audio
        if waveform.ndim != 2 or waveform.shape[1] != 2 or not np.isfinite(waveform).all():
            raise RuntimeError("Music 3 returned invalid stereo audio")
        on_stage("saving audio", 0.95)
        # Preserve the generated stereo signal; the engine already clips to [-1, 1].
        pcm = (waveform * 32767).round().astype("<i2")
        with destination.open("xb") as handle:
            with wave.open(handle, "wb") as writer:
                writer.setnchannels(2)
                writer.setsampwidth(2)
                writer.setframerate(44_100)
                writer.writeframes(pcm.tobytes())
            handle.flush()
            os.fsync(handle.fileno())
        return {"processingSeconds": time.monotonic() - started, "plannerSeconds": planner_seconds,
                "peakMemoryGb": mx.get_peak_memory() / 1e9, "audioFrames": frame_count}


class Music3State:
    def __init__(self, runtime_root: Path, model_root: Path, engine_factory: Callable = MlxMusic3Engine):
        self.runtime_root = runtime_root
        self.output_root = runtime_root / "outputs"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.registry_path = runtime_root / "tasks.json"
        self.model_root = model_root
        self.engine_factory = engine_factory
        self.lock = threading.RLock()
        self.pending: queue.Queue[str | None] = queue.Queue(maxsize=4)
        self.ready = False
        self.real_inference = False
        self.loading_error: str | None = None
        self.runtime_version: str | None = None
        self.precision: dict[str, Any] | None = None
        self.actual_memory: dict[str, Any] | None = None
        self.tasks: dict[str, dict[str, Any]] = {}
        if self.registry_path.is_file():
            loaded = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Music 3 task registry is invalid")
            self.tasks = loaded
            for task in self.tasks.values():
                if task.get("status") == 0:
                    task.update(status=2, stage="interrupted by server restart", error="Generation was interrupted by a server restart")
            self._save()
        self.worker = threading.Thread(target=self._run, name="music3-generation", daemon=True)

    def _save(self) -> None:
        atomic_json(self.registry_path, self.tasks)

    def start(self) -> None:
        self.worker.start()

    def health(self) -> dict[str, Any]:
        with self.lock:
            return {"status": "error" if self.loading_error else "ok", "engine": "minimax-music3",
                "models_initialized": self.ready, "llm_initialized": self.ready,
                "loaded_model": MODEL_ID, "loaded_lm_model": None, "model_revision": MODEL_REVISION,
                "runtime_version": self.runtime_version, "runtime_commit": UPSTREAM_COMMIT,
                "precision": self.precision, "capabilities": CAPABILITIES.copy(), "max_duration_seconds": MAX_DURATION,
                "maxDurationSeconds": MAX_DURATION, "realInference": self.ready and self.real_inference,
                "inference_backend": "mlx" if self.real_inference else None,
                "memory_policy": MEMORY_POLICY, "actualMemory": self.actual_memory,
                "stage": "failed" if self.loading_error else "ready" if self.ready else "loading model",
                "error": self.loading_error}

    def submit(self, payload: Any) -> str:
        request = validate_request(payload)
        with self.lock:
            if not self.ready:
                raise RuntimeError(self.loading_error or "Music 3 is still loading its model")
            if self.pending.full():
                raise RuntimeError("Music 3 generation queue is full")
            task_id = uuid.uuid4().hex
            self.tasks[task_id] = {"taskId": task_id, "status": 0, "stage": "queued", "progress": 0.0,
                                   "request": request, "createdAt": time.time()}
            self._save()
            self.pending.put_nowait(task_id)
            return task_id

    def query(self, task_ids: Any) -> list[dict[str, Any]]:
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 16 or any(not isinstance(value, str) or len(value) != 32 or any(char not in "0123456789abcdef" for char in value) for value in task_ids):
            raise ValueError("task_id_list must contain 1 to 16 opaque task IDs")
        rows = []
        with self.lock:
            for task_id in task_ids:
                if task_id not in self.tasks:
                    raise KeyError("Unknown Music 3 task")
                task = self.tasks[task_id]
                result: dict[str, Any] = {"progress": task["progress"], "stage": task["stage"],
                    "seed_value": task["request"]["seed"], "dit_model": MODEL_ID, "lm_model": None,
                    "metas": task.get("metas", {}), "generation_info": task.get("generationInfo", "")}
                if task.get("error"):
                    result["error"] = task["error"]
                if task["status"] == 1:
                    result["file"] = "/v1/audio?artifact_id=" + task_id
                rows.append({"task_id": task_id, "status": task["status"], "result": json.dumps([result], ensure_ascii=False), "progress_text": task["stage"]})
        return rows

    def artifact(self, artifact_id: str) -> Path:
        with self.lock:
            task = self.tasks.get(artifact_id)
            if not task or task.get("status") != 1:
                raise KeyError("Unknown completed Music 3 artifact")
            path = self.output_root / f"{artifact_id}.wav"
            identity = task.get("artifact", {})
        if not path.is_file() or path.is_symlink() or path.stat().st_size != identity.get("bytes"):
            raise KeyError("Music 3 artifact is missing or changed")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != identity.get("sha256"):
            raise KeyError("Music 3 artifact is missing or changed")
        return path

    def _run(self) -> None:
        try:
            engine = self.engine_factory(self.model_root)
            try:
                from importlib.metadata import version
                runtime_version = version("mlx-audio")
            except Exception:
                runtime_version = None
            with self.lock:
                self.runtime_version = runtime_version
                self.real_inference = getattr(engine, "real_inference", False) is True
                self.precision = getattr(engine, "precision", None)
                self.actual_memory = getattr(engine, "last_memory", None)
                self.ready = True
            if self.real_inference:
                def observe_memory(snapshot: dict[str, Any]):
                    with self.lock:
                        self.actual_memory = snapshot.copy()
                engine.memory_observer = observe_memory
            print("Music 3 checkpoint verified; generation worker is ready", flush=True)
        except Exception as error:
            with self.lock:
                self.loading_error = f"{type(error).__name__}: {error}"
            traceback.print_exc()
            return
        while True:
            task_id = self.pending.get()
            if task_id is None:
                return
            with self.lock:
                task = self.tasks[task_id]
                payload = task["request"].copy()
                task.update(stage="starting generation", startedAt=time.time())
                self._save()
            temporary = self.output_root / f".{task_id}.partial.wav"
            destination = self.output_root / f"{task_id}.wav"

            def on_stage(stage: str, progress: float) -> None:
                with self.lock:
                    self.tasks[task_id].update(stage=stage, progress=progress)

            try:
                metadata = engine.generate(payload, temporary, on_stage)
                identity = audio_identity(temporary)
                os.link(temporary, destination)
                temporary.unlink()
                directory = os.open(self.output_root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                with self.lock:
                    task.update(status=1, stage="completed", progress=1.0, completedAt=time.time(), artifact=identity,
                        metas={"engine": "minimax-music3", "model": MODEL_ID, "modelRevision": MODEL_REVISION,
                            "runtimeCommit": UPSTREAM_COMMIT, "runtimeVersion": self.runtime_version,
                            "precision": self.precision,
                            "memoryPolicy": MEMORY_POLICY, "actualMemory": self.actual_memory,
                            "requestedDurationSeconds": payload["audio_duration"], "duration": identity["durationSeconds"],
                            "sampleRate": identity["sampleRate"], "channels": identity["channels"],
                            "steps": payload["inference_steps"], "seed": payload["seed"], **metadata},
                        generationInfo="MiniMax Music 3 native MLX; no source audio conditioning; duration is a maximum and the model may finish earlier")
                    self._save()
            except Exception as error:
                temporary.unlink(missing_ok=True)
                with self.lock:
                    task.update(status=2, stage="generation failed", completedAt=time.time(), error=f"{type(error).__name__}: {error}")
                    self._save()
                traceback.print_exc()
            finally:
                self.pending.task_done()


class Music3HttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: Music3State, api_key: str):
        self.state = state
        self.authorization = "Bearer " + _validate_key(api_key)
        if address[0] == "::1":
            self.address_family = socket.AF_INET6
        super().__init__(address, Music3Handler)


class Music3Handler(BaseHTTPRequestHandler):
    server: Music3HttpServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format: str, *args: Any) -> None:
        # URLs contain only opaque IDs, but credentials and private lyrics never
        # appear in access logs. Keep task state in the private runtime registry.
        pass

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorize(self) -> bool:
        supplied = self.headers.get_all("Authorization", [])
        if len(supplied) != 1 or not supplied[0].isascii() or not hmac.compare_digest(supplied[0], self.server.authorization):
            self._json(401, {"error": "Music 3 API authentication required"})
            return False
        if self.headers.get_all("Origin"):
            self._json(403, {"error": "Browser access is disabled"})
            return False
        return True

    def _body(self) -> Any:
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            raise ValueError("Only a bounded application/json request is supported")
        length = self.headers.get("Content-Length")
        if length is None or not length.isdecimal() or not 1 <= int(length) <= MAX_BODY_BYTES:
            raise ValueError("Invalid or oversized request body")
        return json.loads(self.rfile.read(int(length)), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON numbers are unsupported")))

    def do_GET(self) -> None:
        if not self._authorize():
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health" and not parsed.query:
            self._json(200, {"data": self.server.state.health()})
            return
        if parsed.path != "/v1/audio":
            self._json(404, {"error": "API route is unavailable"})
            return
        query = urllib.parse.parse_qs(parsed.query)
        ids = query.get("artifact_id", [])
        if set(query) != {"artifact_id"} or len(ids) != 1 or len(ids[0]) != 32 or any(char not in "0123456789abcdef" for char in ids[0]):
            self._json(400, {"error": "An opaque artifact_id is required"})
            return
        try:
            path = self.server.state.artifact(ids[0])
        except KeyError as error:
            self._json(404, {"error": str(error)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                self.wfile.write(block)

    def do_POST(self) -> None:
        if not self._authorize():
            return
        if self.path not in {"/release_task", "/query_result"}:
            self._json(404, {"error": "API route is unavailable"})
            return
        try:
            payload = self._body()
            if self.path == "/release_task":
                self._json(200, {"data": {"task_id": self.server.state.submit(payload)}})
            else:
                if not isinstance(payload, dict) or set(payload) != {"task_id_list"}:
                    raise ValueError("query_result requires only task_id_list")
                self._json(200, {"data": self.server.state.query(payload["task_id_list"])})
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
        except KeyError as error:
            self._json(404, {"error": str(error)})
        except RuntimeError as error:
            self._json(503, {"error": str(error)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost", "::1"])
    parser.add_argument("--port", default=18002, type=int)
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    acoustic_dtype = os.environ.get("MUSIC_ENGINE_MUSIC3_ACOUSTIC_DTYPE", "float32")
    if acoustic_dtype not in ACOUSTIC_DTYPES:
        parser.error(f"MUSIC_ENGINE_MUSIC3_ACOUSTIC_DTYPE must be one of {', '.join(ACOUSTIC_DTYPES)}")
    with RuntimeOwner(RUNTIME_ROOT):
        state = Music3State(RUNTIME_ROOT, MODEL_ROOT, lambda root: MlxMusic3Engine(root, acoustic_dtype=acoustic_dtype))
        server = Music3HttpServer((options.host, options.port), state, ensure_api_key())
        state.start()
        print(f"Music 3 API listening on http://{options.host}:{options.port}; loading model", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
