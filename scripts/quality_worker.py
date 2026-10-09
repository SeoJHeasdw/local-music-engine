#!/usr/bin/env python3
"""Isolated local music analysis; only ``setup`` accesses model download URLs.

No expected lyrics enter this process. Source audio is read, never rewritten or
uploaded. Learned vocal estimates and speech-recognizer confidence are evidence,
not musical judgments or independent votes. Libraries are imported lazily so the
project's standard-library Python can inspect readiness and run contract tests.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
from importlib.metadata import version as package_version
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "quality"
MODELS = ROOT / ".runtime" / "models" / "quality"
WORKER_VERSION = "local-quality-v1"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
WHISPER_REVISION = "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
WHISPER_DIRECTORY = MODELS / "whisper-large-v3-turbo" / WHISPER_REVISION
UMX_DIRECTORY = MODELS / "umxhq"
UMX_NAME = "vocals-b62c91ce.pth"
UMX_DRUMS_NAME = "drums-9619578f.pth"
UMX_DRUMS_FILE = (UMX_DIRECTORY / UMX_DRUMS_NAME,
    "https://zenodo.org/records/3370489/files/drums-9619578f.pth?download=1",
    "9619578f885c54737cb0234f9f9a4a679ee4f31438fd77fd1dbe02bb16c2da0a")
MODEL_FILES = (
    (WHISPER_DIRECTORY / "config.json", f"https://huggingface.co/{WHISPER_MODEL}/resolve/{WHISPER_REVISION}/config.json", "b34fc29e4e11e0a25e812775dd67f4dd16fc2c8eb43d28ae25ff7d660ecb6379"),
    (WHISPER_DIRECTORY / "weights.safetensors", f"https://huggingface.co/{WHISPER_MODEL}/resolve/{WHISPER_REVISION}/weights.safetensors", "951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6"),
    (UMX_DIRECTORY / UMX_NAME, f"https://zenodo.org/records/3370489/files/{UMX_NAME}?download=1", "b62c91cedbc7a066f1778ead5b5cecb377aa3a46a31af1cce7c5c8769339d083"),
)
SEPARATOR_CHUNK_SECONDS = 20.0
SEPARATOR_CONTEXT_SECONDS = 0.5
ACTIVITY_WINDOW_SECONDS = 0.25
MAX_AUDIO_SECONDS = 610.0
PARENT_PID_ENV = "MUSIC_ENGINE_QUALITY_PARENT_PID"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def initiating_parent_alive(pid: int, *, direct_parent: bool, getppid=None, check_pid=None) -> bool:
    """Direct workers also detect reparenting, including a reused initiating PID."""
    getppid = getppid or os.getppid
    check_pid = check_pid or os.kill
    if direct_parent and getppid() != pid:
        return False
    try:
        check_pid(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # An existing process under another effective user is still alive.
        pass
    return True


@contextlib.contextmanager
def parent_watchdog(command: str, *, environ=None, parent_check=None, on_exit=None, interval: float = 0.25):
    """Only managed invocations die with their initiating CLI; manual runs persist.

    The daemon uses os._exit because the parent can disappear during native model
    inference or a blocking lock wait. The OS releases memory and file locks even
    when the Python/native stack cannot cooperatively cancel at that point.
    Optional callbacks let contract tests exercise this without loading models.
    """
    environment = os.environ if environ is None else environ
    raw_pid = environment.get(PARENT_PID_ENV)
    if not raw_pid:
        yield
        return
    pid = int(raw_pid)
    if pid <= 1:
        raise ValueError(f"{PARENT_PID_ENV} must contain an initiating process PID")
    check = parent_check or initiating_parent_alive
    exit_process = on_exit or os._exit
    direct_parent = command != "setup"
    stop = threading.Event()

    def parent_gone() -> None:
        log("Initiating music process exited; stopping local quality work")
        exit_process(125)

    if not check(pid, direct_parent=direct_parent):
        parent_gone()
        # The real exit callback never returns. Keep injected callbacks from
        # allowing analysis to start after an already missing parent.
        raise RuntimeError("Initiating music process has exited")

    def monitor() -> None:
        while not stop.wait(interval):
            if not check(pid, direct_parent=direct_parent):
                parent_gone()
                return

    watcher = threading.Thread(target=monitor, name="quality-parent-watchdog", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join(timeout=1.0)


@contextlib.contextmanager
def analysis_lock():
    """Wait model-free on a persistent inode; kernel release handles SIGKILL."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "analysis.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Waiting for the current local quality analysis to finish")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def readiness(*, verify_hashes: bool = False) -> dict[str, Any]:
    missing: list[str] = []
    invalid: list[str] = []
    for path, _url, expected_hash in MODEL_FILES:
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(str(path.relative_to(MODELS)))
        elif verify_hashes and sha256(path) != expected_hash:
            invalid.append(str(path.relative_to(MODELS)))
    return {"available": not missing and not invalid, "backend": WORKER_VERSION,
            "model": WHISPER_MODEL, "modelRevision": WHISPER_REVISION,
            "missingModels": missing, "invalidModels": invalid}


def download_model(path: Path, url: str, expected_hash: str) -> None:
    if path.is_file() and sha256(path) == expected_hash:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "local-music-engine/quality-v1"})
        with os.fdopen(descriptor, "wb") as stream, urllib.request.urlopen(request, timeout=90) as response:
            digest = hashlib.sha256()
            written = 0
            last_progress = 0
            while block := response.read(1024 * 1024):
                stream.write(block)
                digest.update(block)
                written += len(block)
                if written - last_progress >= 64 * 1024 * 1024:
                    log(f"Downloading {path.name}: {written // (1024 * 1024)} MiB")
                    last_progress = written
            stream.flush()
            os.fsync(stream.fileno())
        if digest.hexdigest() != expected_hash:
            raise RuntimeError(f"Model checksum mismatch: {path.name}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def setup(*, include_drums: bool = False) -> dict[str, Any]:
    """Explicit bootstrap-only download; verified immutable model identities."""
    for path, url, expected_hash in MODEL_FILES:
        log(f"Preparing {path.parent.name}/{path.name}")
        download_model(path, url, expected_hash)
    if include_drums:
        download_model(*UMX_DRUMS_FILE)
    result = readiness(verify_hashes=True)
    result["licenses"] = {"whisper": "MIT", "mlx-whisper": "MIT", "umxhq": "MIT"}
    result["modelFiles"] = [{"path": str(path.relative_to(MODELS)), "sha256": digest}
                            for path, _url, digest in MODEL_FILES]
    result["requirementsSha256"] = sha256(ROOT / "scripts" / "quality-requirements.txt")
    result["pythonVersion"] = platform.python_version()
    result["packages"] = {name: package_version(name) for name in ("mlx-whisper", "mlx", "torch", "openunmix", "soundfile")}
    atomic_json(RUNTIME / "ready.json", result)
    return result


def parse_loudness(stderr: str) -> dict[str, Any]:
    """Only input measurements count; output loudnorm measurements are discarded."""
    match = stderr.rfind('{\n\t"input_i"')
    if match < 0:
        # Some FFmpeg releases use spaces rather than tabs.
        match = stderr.rfind("{")
    if match < 0:
        return {"status": "unknown", "reason": "FFmpeg did not return loudness measurements"}
    try:
        values, _end = json.JSONDecoder().raw_decode(stderr[match:])
    except (ValueError, TypeError):
        return {"status": "unknown", "reason": "FFmpeg loudness measurements could not be parsed"}
    fields = {"integratedLufs": "input_i", "loudnessRangeLu": "input_lra",
              "truePeakDbtp": "input_tp", "thresholdLufs": "input_thresh"}
    result = {key: finite(values.get(source)) for key, source in fields.items()}
    result.update(status="measured" if all(value is not None for value in result.values()) else "inconclusive",
                  method="FFmpeg loudnorm input measurement, analysis only")
    return result


def loudness(audio: Path) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"status": "unavailable", "reason": "FFmpeg is not installed"}
    try:
        process = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(audio),
                                  "-map", "0:a:0", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                                  "-f", "null", "-"], capture_output=True, text=True, timeout=120, check=False)
        if process.returncode:
            return {"status": "unknown", "reason": "FFmpeg could not measure this audio"}
        return parse_loudness(process.stderr)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "unknown", "reason": type(error).__name__}


def vocal_summary(windows: list[dict[str, float]], duration: float) -> dict[str, Any]:
    """Summarize *estimated* source energy; no fixed desired vocal/mix balance."""
    if not windows:
        return {"status": "inconclusive", "reason": "No separator analysis windows"}
    peak_rms = max(window["vocalRms"] for window in windows)
    threshold = max(10 ** (-55 / 20), peak_rms * 0.1)
    active = [window for window in windows if window["vocalRms"] >= threshold]
    vocal_energy = sum(window["vocalRms"] ** 2 * (window["endSeconds"] - window["startSeconds"]) for window in windows)
    mix_energy = sum(window["mixRms"] ** 2 * (window["endSeconds"] - window["startSeconds"]) for window in windows)
    ratio = 10 * math.log10(vocal_energy / mix_energy) if vocal_energy > 0 and mix_energy > 0 else None
    activity: list[dict[str, float]] = []
    for window in active:
        if activity and abs(activity[-1]["endSeconds"] - window["startSeconds"]) < 1e-6:
            activity[-1]["endSeconds"] = window["endSeconds"]
        else:
            activity.append({"startSeconds": window["startSeconds"], "endSeconds": window["endSeconds"]})
    active_seconds = sum(window["endSeconds"] - window["startSeconds"] for window in active)
    ending = [window for window in windows if window["endSeconds"] > max(0, duration - 0.75)]
    return {"status": "measured", "model": "UMXHQ vocals", "modelRevision": "zenodo:3370489-v1.0.1",
            "device": "cpu", "chunkSeconds": SEPARATOR_CHUNK_SECONDS,
            "activityThresholdRms": threshold, "vocalActivityFraction": active_seconds / duration if duration else 0.0,
            "vocalToMixDb": ratio, "vocalActiveAtEnd": any(window["vocalRms"] >= threshold for window in ending),
            "vocalActivityRegions": activity[:128], "vocalActivityRegionCount": len(activity),
            "confidence": "estimated", "evidenceOnly": True,
            "caveat": "Source separation may leak instruments or discard vocals. Energy does not establish diction, balance quality, or a complete musical ending."}


def drum_frame_features(drums, *, offset: float, core_start: float, core_end: float, previous=None):
    """Compact attributed onset measurements; centered windows use separator context."""
    import numpy as np
    rate, hop, fft_size = 44100, 441, 2048
    padded = np.pad(drums, ((fft_size // 2, fft_size // 2), (0, 0)))
    times = offset + np.arange(1 + (len(padded) - fft_size) // hop) * hop / rate
    selected = (times >= core_start - 1e-8) & (times < core_end - 1e-8)
    power = None
    energy = None
    for channel in range(drums.shape[1]):
        views = np.lib.stride_tricks.sliding_window_view(padded[:, channel], fft_size)[::hop][selected]
        spectrum = np.abs(np.fft.rfft(views * np.hanning(fft_size), axis=1)) ** 2
        local_energy = np.mean(views ** 2, axis=1)
        power = spectrum if power is None else power + spectrum
        energy = local_energy if energy is None else energy + local_energy
    if power is None or not len(power):
        return np.empty((0, 8)), previous
    magnitude = np.sqrt(power / drums.shape[1])
    magnitude[:, 0] = 0
    preceding = np.vstack((np.zeros(magnitude.shape[1]) if previous is None else previous, magnitude[:-1]))
    denominator = magnitude.sum(axis=1)
    flux = np.divide(np.maximum(0, magnitude - preceding).sum(axis=1), denominator,
                     out=np.zeros(len(magnitude)), where=denominator > 0)
    rms = np.sqrt(energy / drums.shape[1])
    flux[rms < 0.0001] = 0
    features = np.zeros((len(magnitude), 8))
    features[:, 0], features[:, 1], features[:, 7] = times[selected], rms, flux
    return features, magnitude[-1]


def drum_timing_summary(features, *, duration: float, source_hash: str) -> dict[str, Any]:
    import numpy as np
    sys.path.insert(0, str(ROOT / "src"))
    from local_music_engine.music_structure import _tempo, _onsets
    from local_music_engine.percussion_analysis import VERSION as PERCUSSION_VERSION, _supported_beat_candidates
    tempo = _tempo(features[:, 7], 0.01)
    peaks, _ = _onsets(features[:, 7], 0.01)
    segments = []
    for start in range(0, len(features), 1200):
        local = features[start:start + 1200]
        if len(local) < 600:
            continue
        estimate = _tempo(local[:, 7], 0.01)
        segments.append({"startSeconds": round(float(local[0, 0]), 4),
            "endSeconds": round(min(duration, float(local[-1, 0]) + 0.01), 4),
            "bpm": estimate["bpm"], "confidence": estimate["confidence"], "onsetCount": estimate["onsetCount"]})
    return {"version": PERCUSSION_VERSION, "status": "observed" if tempo["bpm"] is not None else "unknown",
        "sourceArtifactSha256": source_hash, "durationSeconds": duration,
        "source": {"kind": "isolated_percussion", "method": "UMXHQ estimated drums", "reliability": 0.75,
            "modelRevision": "zenodo:3370489-v1.0.1", "modelSha256": UMX_DRUMS_FILE[2],
            "caveat": "Drum estimates may leak other instruments; pulse is not a verified quarter-note beat."},
        "pulse": {"bpm": tempo["bpm"], "confidence": tempo["confidence"]}, "segments": segments,
        "onsetTimesSeconds": [round(float(features[index, 0]), 4) for index in peaks],
        "beatCandidatesSeconds": _supported_beat_candidates(features, tempo, segments),
        "method": "model-estimated-drum-onset-observations"}


def separation(audio: Path) -> dict[str, Any]:
    import numpy as np
    import soundfile as sf
    import torch
    from scipy.signal import resample_poly
    from openunmix.model import OpenUnmix, Separator
    from openunmix.utils import bandwidth_to_max_bin

    torch.set_num_threads(min(4, os.cpu_count() or 1))
    target = OpenUnmix(nb_bins=2049, nb_channels=2, hidden_size=512,
                      max_bin=bandwidth_to_max_bin(rate=44100.0, n_fft=4096, bandwidth=16000))
    weights = torch.load(UMX_DIRECTORY / UMX_NAME, map_location="cpu", weights_only=True)
    # Author-published 2019 weights contain these obsolete preprocessing buffers;
    # current Open-Unmix keeps the STFT in Separator. All learned parameters still
    # load strictly, instead of silently accepting a partially loaded network.
    for legacy_buffer in ("sample_rate", "stft.window", "transform.0.window"):
        weights.pop(legacy_buffer, None)
    target.load_state_dict(weights, strict=True)
    target.eval()
    targets = {"vocals": target}
    if UMX_DRUMS_FILE[0].is_file() and sha256(UMX_DRUMS_FILE[0]) == UMX_DRUMS_FILE[2]:
        drums_target = OpenUnmix(nb_bins=2049, nb_channels=2, hidden_size=512,
            max_bin=bandwidth_to_max_bin(rate=44100.0, n_fft=4096, bandwidth=16000))
        drum_weights = torch.load(UMX_DRUMS_FILE[0], map_location="cpu", weights_only=True)
        for legacy_buffer in ("sample_rate", "stft.window", "transform.0.window"):
            drum_weights.pop(legacy_buffer, None)
        drums_target.load_state_dict(drum_weights, strict=True)
        targets["drums"] = drums_target.eval()
    model = Separator(target_models=targets, niter=1, residual=True,
                      n_fft=4096, n_hop=1024, nb_channels=2, sample_rate=44100.0,
                      wiener_win_len=300, filterbank="torch").eval()
    source_hash = sha256(audio)
    windows: list[dict[str, float]] = []
    drum_features, previous_drum = [], None
    with sf.SoundFile(audio) as source, torch.inference_mode():
        rate = source.samplerate
        duration = len(source) / rate
        if duration <= 0 or duration > MAX_AUDIO_SECONDS:
            raise ValueError("Quality analysis supports audio up to 610 seconds")
        chunk_frames = round(SEPARATOR_CHUNK_SECONDS * rate)
        context_frames = round(SEPARATOR_CONTEXT_SECONDS * rate)
        for core_start in range(0, len(source), chunk_frames):
            core_end = min(len(source), core_start + chunk_frames)
            read_start = max(0, core_start - context_frames)
            read_end = min(len(source), core_end + context_frames)
            source.seek(read_start)
            original = source.read(read_end - read_start, dtype="float32", always_2d=True)
            if original.shape[1] > 2:
                raise ValueError("Vocal analysis supports mono or stereo audio")
            if original.shape[1] == 1:
                original = np.repeat(original, 2, axis=1)
            if rate != 44100:
                divisor = math.gcd(rate, 44100)
                original = resample_poly(original, 44100 // divisor, rate // divisor, axis=0)
            samples = torch.from_numpy(np.ascontiguousarray(original.T)).unsqueeze(0)
            estimates = model(samples)[0].cpu().numpy()
            estimate = estimates[0].T
            if "drums" in targets:
                features, previous_drum = drum_frame_features(estimates[1].T, offset=read_start / rate,
                    core_start=core_start / rate, core_end=core_end / rate, previous=previous_drum)
                drum_features.append(features)
            # Ignore context edges; each source frame is measured exactly once.
            trim_start = round((core_start - read_start) * 44100 / rate)
            trim_end = trim_start + round((core_end - core_start) * 44100 / rate)
            vocal = estimate[trim_start:trim_end]
            mixed = original[trim_start:trim_end]
            accompaniment = mixed - vocal
            step = round(ACTIVITY_WINDOW_SECONDS * 44100)
            for start in range(0, len(vocal), step):
                end = min(len(vocal), start + step)
                if end <= start:
                    continue
                windows.append({"startSeconds": core_start / rate + start / 44100,
                                "endSeconds": min(duration, core_start / rate + end / 44100),
                                "vocalRms": float(np.sqrt(np.mean(vocal[start:end] ** 2))),
                                "mixRms": float(np.sqrt(np.mean(mixed[start:end] ** 2))),
                                "accompanimentRms": float(np.sqrt(np.mean(accompaniment[start:end] ** 2)))})
            log(f"Vocal analysis: {core_end / rate:.1f}/{duration:.1f} seconds")
    if sha256(audio) != source_hash:
        raise ValueError("Audio changed during separated frame measurement")
    result = vocal_summary(windows, duration)
    if drum_features:
        result["percussionEvidence"] = drum_timing_summary(np.concatenate(drum_features), duration=duration, source_hash=source_hash)
    # These already-computed residuals add no model, model invocation or remix.
    result["frameEvidence"] = {"version": "separated-energy-v1", "sourceArtifactSha256": source_hash,
        "durationSeconds": duration, "windowSeconds": ACTIVITY_WINDOW_SECONDS,
        "frameSeries": {"columns": ["startSeconds", "endSeconds", "mixRms", "vocalRms", "accompanimentRms"],
            "points": [[window[key] for key in ("startSeconds", "endSeconds", "mixRms", "vocalRms", "accompanimentRms")] for window in windows]},
        "method": "UMXHQ vocal estimate and same-frame mix-minus-vocal residual", "evidenceOnly": True}
    return result


def clean_segments(raw: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    total_characters = 0
    confident_characters = 0
    if not isinstance(raw, list):
        raw = []
    for index, item in enumerate(raw[:2048]):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        start, end = finite(item.get("start")), finite(item.get("end"))
        if start is None or end is None or start < 0 or end <= start or not text:
            continue
        confidence = {key: finite(item.get(key)) for key in ("avg_logprob", "no_speech_prob", "compression_ratio", "temperature")}
        confident = (confidence["avg_logprob"] is not None and confidence["avg_logprob"] >= -1.0
                     and confidence["no_speech_prob"] is not None and confidence["no_speech_prob"] <= 0.6
                     and confidence["compression_ratio"] is not None and confidence["compression_ratio"] <= 2.4)
        total_characters += len(text)
        if confident:
            confident_characters += len(text)
        segments.append({"id": index, "start": start, "end": end, "text": text,
                         **confidence, "reliable": confident})
    fraction = confident_characters / total_characters if total_characters else 0.0
    return segments, {"reliable": total_characters >= 10 and fraction >= 0.7,
                      "confidentTextFraction": fraction, "transcribedCharacters": total_characters,
                      "reliabilityBasis": "Whisper internal confidence only; singing correctness remains a proxy",
                      "confidenceThresholds": {"minimumAvgLogprob": -1.0, "maximumNoSpeechProbability": 0.6,
                                               "maximumCompressionRatio": 2.4, "minimumConfidentTextFraction": 0.7,
                                               "minimumCharacters": 10}}


def transcribe(audio: Path) -> dict[str, Any]:
    import mlx_whisper

    # The expected lyrics are deliberately absent. No network is needed to load
    # a verified local checkpoint. Mixed-language lyrics are not forced to Korean.
    log("Transcribing original mix locally with MLX Whisper")
    output = mlx_whisper.transcribe(str(audio), path_or_hf_repo=str(WHISPER_DIRECTORY),
                                    task="transcribe", language=None, initial_prompt=None,
                                    condition_on_previous_text=False, temperature=0.0,
                                    word_timestamps=False, verbose=None)
    segments, confidence = clean_segments(output.get("segments"))
    return {"available": True, "text": str(output.get("text") or "").strip(),
            "segments": segments, "language": str(output.get("language") or "unknown"),
            **confidence, "transcriptionInput": "original mix", "windowSeconds": 30,
            "independentVotes": 1, "expectedLyricsProvided": False}


def _analyze_unlocked(audio: Path) -> dict[str, Any]:
    if not audio.is_absolute() or not audio.is_file():
        raise ValueError("--audio must name an existing absolute audio file")
    result = {"available": False, "reliable": False, "text": "", "segments": [],
              "language": "unknown", "backend": WORKER_VERSION, "model": WHISPER_MODEL,
              "modelRevision": WHISPER_REVISION, "errors": []}
    log("Measuring loudness without changing the source audio")
    result["loudness"] = loudness(audio)
    ready = readiness(verify_hashes=True)
    if not ready["available"]:
        result["separation"] = {"status": "unavailable", "reason": "Quality models require setup"}
        result["errors"].append("Quality models are missing or have a checksum mismatch; run bootstrap_quality.sh")
        return result
    # Prevent libraries from downloading fallback models or sending telemetry
    # during analysis. Setup is the only network-capable operation in this worker.
    os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                      HF_HOME=str(RUNTIME / "huggingface"), TORCH_HOME=str(RUNTIME / "torch"),
                      NUMBA_CACHE_DIR=str(RUNTIME / "numba"), TOKENIZERS_PARALLELISM="false")
    result["packages"] = {name: package_version(name) for name in ("mlx-whisper", "mlx", "torch", "openunmix", "soundfile")}
    try:
        import soundfile as sf
        info = sf.info(audio)
        if info.duration <= 0 or info.duration > MAX_AUDIO_SECONDS or info.channels not in (1, 2):
            raise ValueError("Quality analysis supports mono/stereo audio between 0 and 610 seconds")
    except Exception as error:
        result["separation"] = {"status": "unknown", "reason": type(error).__name__}
        result["errors"].append(f"Audio analysis unavailable: {type(error).__name__}: {error}")
        return result
    try:
        result["separation"] = separation(audio)
    except Exception as error:
        log(f"Vocal analysis unavailable: {type(error).__name__}: {error}")
        result["separation"] = {"status": "unknown", "reason": type(error).__name__}
        result["errors"].append(f"Vocal analysis unavailable: {type(error).__name__}")
    try:
        result.update(transcribe(audio))
    except Exception as error:
        log(f"Transcription unavailable: {type(error).__name__}: {error}")
        result["errors"].append(f"Transcription unavailable: {type(error).__name__}")
    return result


def analyze(audio: Path) -> dict[str, Any]:
    if not audio.is_absolute() or not audio.is_file():
        raise ValueError("--audio must name an existing absolute audio file")
    # The lock precedes every heavy dependency import in _analyze_unlocked.
    # A second CLI/worker therefore waits without another copy of either model.
    with analysis_lock():
        return _analyze_unlocked(audio)


def separate_only(audio: Path) -> dict[str, Any]:
    """Measure attributed vocal/backing energy without loading the recognizer."""
    if not audio.is_absolute() or not audio.is_file():
        raise ValueError("--audio must name an existing absolute audio file")
    with analysis_lock():
        if not (UMX_DIRECTORY / UMX_NAME).is_file() or sha256(UMX_DIRECTORY / UMX_NAME) != MODEL_FILES[-1][2]:
            raise ValueError("Verified UMXHQ weights are unavailable; prepare the quality environment first")
        os.environ.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                          HF_HOME=str(RUNTIME / "huggingface"), TORCH_HOME=str(RUNTIME / "torch"))
        return {"backend": WORKER_VERSION, "separation": separation(audio), "recognizerLoaded": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_command = commands.add_parser("setup")
    setup_command.add_argument("--drums", action="store_true", help="also prepare the optional pinned UMXHQ drum estimator")
    commands.add_parser("status")
    analyzer = commands.add_parser("analyze")
    analyzer.add_argument("--audio", required=True, type=Path)
    separator = commands.add_parser("separate")
    separator.add_argument("--audio", required=True, type=Path)
    measurement = commands.add_parser("measure")
    measurement.add_argument("--audio", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        # Third-party import/model output is always stderr. The sole stdout item
        # is the protocol result, even when libraries print startup diagnostics.
        with parent_watchdog(arguments.command), contextlib.redirect_stdout(sys.stderr):
            if arguments.command == "setup":
                if platform.system() != "Darwin" or platform.machine() not in {"arm64", "aarch64"}:
                    raise RuntimeError("The quality toolchain requires Apple Silicon macOS")
                result = setup(include_drums=True) if arguments.drums else setup()
            elif arguments.command == "status":
                result = readiness()
            elif arguments.command == "measure":
                if not arguments.audio.is_absolute() or not arguments.audio.is_file():
                    raise ValueError("--audio must name an existing absolute audio file")
                result = {"loudness": loudness(arguments.audio), "backend": WORKER_VERSION}
            elif arguments.command == "separate":
                result = separate_only(arguments.audio)
            else:
                result = analyze(arguments.audio)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        return 0
    except Exception as error:
        log(f"Quality worker: {type(error).__name__}: {error}")
        print(json.dumps({"available": False, "reliable": False, "backend": WORKER_VERSION,
                          "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False, allow_nan=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
