"""Build new controlled timing experiments from an explicit existing WAV.

No engine, separator, model, listening approval, or natural-audio labels are used.
Only imposed transformations receive labels; baseline/gain/fade cases are unlabelled.
The same time maps can be applied by resampling, which also bends pitch, or by
overlap-add, which moves timing only.
Example:
uv run python scripts/build_rhythm_evaluation.py --source existing.wav \
    --output-dir .runtime/new-corpus --split calibration --source-family song-a
uv run python scripts/build_rhythm_evaluation.py --source existing.wav \
    --output-dir .runtime/new-corpus-pitch-kept --split calibration --source-family song-a \
    --time-method overlap_add
Existing WAVs can be explicitly verified and retained during a truth-only revision:
uv run python scripts/build_rhythm_evaluation.py --refine-existing \
    --manifest .runtime/old/corpus.json --provenance .runtime/old/provenance.json \
    --new-manifest .runtime/old/corpus-support-v2.json \
    --new-provenance .runtime/old/provenance-support-v2.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import wave

# Set before loading numerical libraries in the standalone process.
for _thread_variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[_thread_variable] = "1"

import numpy as np

from local_music_engine.music_structure import _decode
from local_music_engine.percussion_analysis import analyze_percussion
from local_music_engine.rhythm_evaluation import validate_manifest
from local_music_engine.storage import ProjectStore, atomic_write_json, sha256_file, utc_now

VERSION = "rhythm-evaluation-v1"
BUILDER_VERSION = "controlled-rhythm-corpus-v2"
MAX_SECONDS = 60.0
MAX_RATE = 96_000
MAX_PCM_BYTES = 48_000_000
TIME_METHODS = {"resample": "linear_resampling_changes_pitch", "overlap_add": "similarity_overlap_add_preserves_pitch"}
STRETCH_GRAIN_SECONDS = 0.04
STRETCH_TOLERANCE_SECONDS = 0.01


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _read_clip(source: Path, start: float, seconds: float) -> tuple[bytes, dict[str, Any]]:
    with wave.open(str(source), "rb") as audio:
        rate, channels, width, total = audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()
        if audio.getcomptype() != "NONE" or width not in (1, 2, 3, 4) or not 1 <= channels <= 2 or not 8000 <= rate <= MAX_RATE:
            raise ValueError("Corpus input requires mono/stereo 8/16/24/32-bit PCM WAV at 8–96 kHz")
        first = round(start * rate)
        count = min(round(seconds * rate), total - first)
        if first < 0 or count < 24 * rate:
            raise ValueError("Explicit source clip must contain at least 24 seconds")
        if count * channels * width > MAX_PCM_BYTES:
            raise ValueError("Source clip exceeds the bounded PCM memory budget")
        audio.setpos(first)
        pcm = audio.readframes(count)
        if len(pcm) != count * channels * width:
            raise ValueError("Source WAV PCM is truncated")
    return pcm, {"sampleRate": rate, "channels": channels, "sampleWidthBytes": width,
                 "frames": count, "sourceFrames": total, "startFrame": first,
                 "startSeconds": first / rate, "durationSeconds": count / rate}


def _atomic_wav(path: Path, pcm: bytes, metadata: dict[str, Any]) -> None:
    """Publish a complete fsynced WAV atomically without replacing an existing file."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            with wave.open(stream, "wb") as audio:
                audio.setnchannels(metadata["channels"])
                audio.setsampwidth(metadata["sampleWidthBytes"])
                audio.setframerate(metadata["sampleRate"])
                audio.writeframes(pcm)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and refuses an existing destination.
        os.link(temporary, path)
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _encode(samples: np.ndarray, width: int) -> bytes:
    maximum = (1 << (8 * width - 1)) - 1
    integers = np.clip(np.rint(samples * (1 << (8 * width - 1))), -maximum - 1, maximum).astype(np.int64)
    if width == 1:
        return (integers + 128).astype("u1").tobytes()
    if width == 3:
        values = integers.reshape(-1).astype(np.int32)
        return np.column_stack((values & 255, (values >> 8) & 255, (values >> 16) & 255)).astype("u1").tobytes()
    return integers.astype({2: "<i2", 4: "<i4"}[width]).tobytes()


def warp_pcm(pcm: bytes, metadata: dict[str, Any], input_knots: np.ndarray,
             output_knots: np.ndarray) -> bytes:
    """Inverse-map all channels; preserve every PCM byte outside the local edit."""
    if (len(input_knots) != len(output_knots) or len(input_knots) < 3
            or np.any(np.diff(input_knots) <= 0) or np.any(np.diff(output_knots) <= 0)
            or input_knots[0] != output_knots[0] or input_knots[-1] != output_knots[-1]):
        raise ValueError("Time mapping must be ordered, monotonic and unchanged at both boundaries")
    rate, channels, width = metadata["sampleRate"], metadata["channels"], metadata["sampleWidthBytes"]
    first, stop = round(float(input_knots[0]) * rate), round(float(input_knots[-1]) * rate)
    if not 0 <= first < stop <= metadata["frames"]:
        raise ValueError("Time mapping must stay within source clip")
    stride = channels * width
    # Decode only the intervention plus one interpolation neighbor.
    left, right = max(0, first - 1), min(metadata["frames"], stop + 1)
    samples = _decode(pcm[left * stride:right * stride], width, channels)
    output_times = np.arange(first, stop, dtype=float) / rate
    input_positions = np.interp(output_times, output_knots, input_knots) * rate - left
    low = np.floor(input_positions).astype(np.int64)
    high = np.minimum(low + 1, len(samples) - 1)
    fraction = (input_positions - low)[:, None]
    transformed = samples[low] * (1 - fraction) + samples[high] * fraction
    return pcm[:first * stride] + _encode(transformed, width) + pcm[stop * stride:]


def stretch_pcm(pcm: bytes, metadata: dict[str, Any], input_knots: np.ndarray,
                output_knots: np.ndarray) -> bytes:
    """Follow the same time map by overlap-adding unresampled grains: timing moves, pitch does not.

    Each 40 ms grain is taken within 10 ms of its mapped position, where it best
    continues the previous grain. Hits are therefore displaced as mapped, to
    within that tolerance, and every PCM byte outside the edit is preserved.
    """
    if (len(input_knots) != len(output_knots) or len(input_knots) < 3
            or np.any(np.diff(input_knots) <= 0) or np.any(np.diff(output_knots) <= 0)
            or input_knots[0] != output_knots[0] or input_knots[-1] != output_knots[-1]):
        raise ValueError("Time mapping must be ordered, monotonic and unchanged at both boundaries")
    rate, channels, width = metadata["sampleRate"], metadata["channels"], metadata["sampleWidthBytes"]
    first, stop = round(float(input_knots[0]) * rate), round(float(input_knots[-1]) * rate)
    grain = 2 * round(STRETCH_GRAIN_SECONDS * rate / 2)
    step, reach, blend = grain // 2, round(STRETCH_TOLERANCE_SECONDS * rate), round(0.005 * rate)
    left, right = first - 2 * grain - reach, stop + 2 * grain + reach
    if not 0 <= left < first < stop < right <= metadata["frames"] or stop - first < 4 * grain:
        raise ValueError("Time mapping needs room for overlap-add grains inside the source clip")
    stride = channels * width
    samples = _decode(pcm[left * stride:right * stride], width, channels)
    mono = samples.mean(axis=1)
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(grain) / grain)
    output = np.zeros_like(samples)
    weight = np.zeros(len(samples))
    previous: int | None = None
    for start in range(first - grain - left, stop + grain - left, step):
        centre = (start + left + grain / 2) / rate
        mapped = round(float(np.interp(centre, output_knots, input_knots)) * rate - grain / 2) - left
        chosen = mapped
        if previous is not None and previous + step != mapped:
            target = mono[previous + step:previous + step + grain]
            search = mono[mapped - reach:mapped + reach + grain]
            chosen = mapped - reach + int(np.argmax(np.correlate(search, target, "valid")))
        output[start:start + grain] += samples[chosen:chosen + grain] * window[:, None]
        weight[start:start + grain] += window
        previous = chosen
    a, b = first - left, stop - left
    edited = output[a:b] / np.maximum(weight[a:b], 1e-9)[:, None]
    # The map is the identity at both ends; a 5 ms blend keeps the joins sample-continuous.
    fade = np.minimum(1, np.minimum(np.arange(b - a) + 1, np.arange(b - a, 0, -1)) / blend)[:, None]
    edited = samples[a:b] * (1 - fade) + edited * fade
    return pcm[:first * stride] + _encode(edited, width) + pcm[stop * stride:]


def timing_mapping(start: float, seconds: float, strength_ms: float, *, kind: str,
                   observed_period: float | None = None,
                   observed_anchor: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """An input→output map supplies known intervention truth, never detector features."""
    amplitude = strength_ms / 1000
    spacing = max(0.4, observed_period or 0.5, amplitude * 2 + 0.025)
    phase = (observed_anchor - start) % spacing if observed_anchor is not None else 0
    interior = np.arange(start + phase, start + seconds, spacing)
    interior = interior[(interior > start + amplitude + 0.005) & (interior < start + seconds - amplitude - 0.005)]
    source = np.r_[start, interior, start + seconds]
    count = len(source) - 1
    delays = np.zeros(len(source))
    if kind == "alternating":
        delays[1:-1] = amplitude * np.where(np.arange(1, count) % 2, 1, -1)
    elif kind == "burst":
        center = count // 2
        delays[max(1, center - 1)] = -amplitude
        delays[center] = amplitude
        if center + 1 < count:
            delays[center + 1] = -amplitude
    elif kind == "smooth_tempo":
        # Dense smooth timing curve: gradual acceleration/deceleration,
        # returning to the original phase and duration without a splice.
        source = np.linspace(start, start + seconds, max(65, round(seconds * 40) + 1))
        delays = amplitude * np.sin(np.linspace(0, np.pi, len(source))) ** 2
        delays[[0, -1]] = 0
    else:
        raise ValueError("Unsupported time perturbation")
    target = source + delays
    if np.any(np.diff(target) <= 0):
        raise ValueError("Requested timing perturbation would reverse audio time")
    return source, target


def time_map_support(input_knots: Any, output_knots: Any) -> list[tuple[float, float]]:
    """Return exact nonidentity piecewise-linear map support from authored knots.

    A segment changes time if either endpoint has a nonzero displacement. Its
    outer boundaries are the immediate identity knots, not the first/last
    displaced onset, an audio detector window, or the broad editing interval.
    """
    source, target = np.asarray(input_knots, dtype=float), np.asarray(output_knots, dtype=float)
    if (source.ndim != 1 or target.ndim != 1 or len(source) != len(target) or len(source) < 3
            or not np.all(np.isfinite(source)) or not np.all(np.isfinite(target))
            or np.any(np.diff(source) <= 0) or np.any(np.diff(target) <= 0)
            or source[0] != target[0] or source[-1] != target[-1]):
        raise ValueError("Support requires a finite monotone time map with identity boundaries")
    displaced = source != target
    segments = displaced[:-1] | displaced[1:]
    support: list[tuple[float, float]] = []
    first: int | None = None
    for index, changed in enumerate(segments):
        if changed and first is None:
            first = index
        if first is not None and (not changed or index == len(segments) - 1):
            stop = index + 1 if changed else index
            support.append((float(source[first]), float(source[stop])))
            first = None
    return support


def _context(evidence: dict[str, Any], duration: float, seconds: float) -> dict[str, Any]:
    beats = np.asarray(evidence.get("beatCandidatesSeconds", []), dtype=float)
    pulse = evidence.get("pulse", {})
    period = 60 / pulse["bpm"] if pulse.get("bpm") and pulse.get("confidence", 0) >= 0.6 else None
    start = (duration - seconds) / 2
    candidates = [start, *np.linspace(max(1, start - 6), min(duration - seconds - 1, start + 6), 25)]
    best: tuple[float, dict[str, Any]] | None = None
    for proposed in candidates:
        left, right = beats[beats < proposed][-8:], beats[beats >= proposed + seconds][:8]
        stable = (period is not None and len(left) == len(right) == 8
                  and all(np.max(np.abs(np.diff(flank) - period)) <= max(0.018, period * 0.04) for flank in (left, right)))
        item = {"startSeconds": float(proposed), "endSeconds": float(proposed + seconds),
                "measuredFlankPulseCounts": [len(left), len(right)], "eightStablePulsesOnEachFlank": bool(stable),
                "observedPeriodSeconds": period,
                "observedFirstPulseSeconds": float(beats[beats >= proposed][0]) if np.any(beats >= proposed) else None}
        score = float(stable) * 100 - abs(proposed - start)
        if best is None or score > best[0]:
            best = score, item
    assert best is not None
    return best[1]


def _project_source(project_root: Path | None, source: Path, source_hash: str) -> dict[str, Any] | None:
    if project_root is None:
        return None
    store = ProjectStore(project_root)
    project = store.load()
    artifacts = {item["artifactId"]: item for item in project["artifacts"]}
    for candidate in project["candidates"]:
        artifact = artifacts.get(candidate.get("artifactId"), {})
        if candidate.get("status") == "ready" and artifact.get("sha256") == source_hash and artifact.get("bytes") == source.stat().st_size:
            valid, reason = store.verify_artifact(artifact)
            if not valid:
                raise ValueError(reason)
            return {"projectJson": str(store.manifest_path), "candidateId": candidate["candidateId"],
                    "artifactId": artifact["artifactId"], "verification": "completed_candidate_file_bytes_sha256"}
    raise ValueError("Source project has no verified completed candidate matching the explicit source")


def build_corpus(source: Path | str, output_dir: Path | str, *, split: str, source_family: str,
                 source_project: Path | str | None = None, start_seconds: float = 0,
                 clip_seconds: float = 60, strengths_ms: tuple[float, ...] = (50, 90, 130),
                 durations_seconds: tuple[float, ...] = (4, 6), kinds: tuple[str, ...] = ("alternating", "burst"),
                 time_method: str = "resample") -> dict[str, Any]:
    source, output = Path(source).expanduser().resolve(), Path(output_dir).expanduser().resolve()
    start_seconds, clip_seconds = _finite(start_seconds, "start_seconds"), _finite(clip_seconds, "clip_seconds")
    if split not in ("calibration", "holdout") or not isinstance(source_family, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", source_family):
        raise ValueError("Provide a supported split and a short unique source-family name")
    if time_method not in TIME_METHODS:
        raise ValueError("Use the resample or overlap_add time method")
    apply_time_map = warp_pcm if time_method == "resample" else stretch_pcm
    if start_seconds < 0 or not 24 <= clip_seconds <= MAX_SECONDS:
        raise ValueError("Clip length must be 24–60 seconds with a nonnegative explicit start")
    if (not strengths_ms or any(not 0 < _finite(value, "strength") <= 200 for value in strengths_ms)
            or not durations_seconds or any(not 4 <= _finite(value, "duration") <= 6 for value in durations_seconds)
            or not kinds or any(kind not in ("alternating", "burst") for kind in kinds)):
        raise ValueError("Use timing strengths in (0, 200] ms, durations in [4, 6] seconds, and alternating/burst kinds")
    if len(set(strengths_ms)) != len(strengths_ms) or len(set(durations_seconds)) != len(durations_seconds) or len(set(kinds)) != len(kinds):
        raise ValueError("Corpus parameter lists must not contain duplicates")
    if len(strengths_ms) * len(durations_seconds) * len(kinds) > 64:
        raise ValueError("Requested corpus exceeds 64 timing interventions")
    source_hash = sha256_file(source)
    project_provenance = _project_source(Path(source_project) if source_project is not None else None, source, source_hash)
    pcm, metadata = _read_clip(source, start_seconds, clip_seconds)
    output.mkdir(parents=True, exist_ok=False)
    baseline = output / "baseline.wav"
    _atomic_wav(baseline, pcm, metadata)
    baseline_hash = sha256_file(baseline)
    measured = analyze_percussion(baseline)
    cases: list[dict[str, Any]] = []
    transforms: list[dict[str, Any]] = []
    source_ref = str(output / "provenance.json")

    def add(name: str, path: Path, *, transform: dict[str, Any], label: str | None = None,
            region: tuple[float, float] | None = None, intent: Path | None = None,
            category: str = "beat_timing", review_margin: float = 0) -> None:
        case_id = f"{source_family}-{name}"
        case = {"caseId": case_id, "split": split, "sourceFamily": source_family,
                "labelOrigin": "controlled_injection", "sourceArtifactSha256": sha256_file(path),
                "parentArtifactSha256": source_hash, "audioPath": str(path), "audioBytes": path.stat().st_size,
                "durationSeconds": metadata["durationSeconds"], "reviewedRanges": [], "annotations": []}
        if intent is not None:
            case["intentPath"] = str(intent)
        if region is not None and label is not None:
            a, b = region
            case["reviewedRanges"] = [{"startSeconds": max(0, a - review_margin),
                                      "endSeconds": min(metadata["durationSeconds"], b + review_margin), "categories": [category]}]
            case["annotations"] = [{"annotationId": case_id + "-" + category, "category": category, "label": label,
                "startSeconds": a, "endSeconds": b, "evidence": {"kind": "controlled_injection",
                    "description": "Known imposed transformation only; natural defects and musical intent remain unknown.",
                    "sourceRef": source_ref + "#" + case_id}}]
        cases.append(case)
        transforms.append({"caseId": case_id, "audioPath": str(path), "sourceArtifactSha256": case["sourceArtifactSha256"],
                           "parentArtifactSha256": source_hash, "baselineArtifactSha256": baseline_hash, **transform})

    add("baseline", baseline, transform={"kind": "identity", "naturalAudioLabel": "unlabelled"})
    channels, width, rate = metadata["channels"], metadata["sampleWidthBytes"], metadata["sampleRate"]
    for seconds in durations_seconds:
        context = _context(measured, metadata["durationSeconds"], seconds)
        a, b = context["startSeconds"], context["endSeconds"]
        for strength in strengths_ms:
            for kind in kinds:
                name = f"{kind}-{strength:g}ms-{seconds:g}s"
                input_knots, output_knots = timing_mapping(a, seconds, strength, kind=kind,
                    observed_period=context["observedPeriodSeconds"], observed_anchor=context["observedFirstPulseSeconds"])
                path = output / f"{name}.wav"
                variant_pcm = apply_time_map(pcm, metadata, input_knots, output_knots)
                waveform_changed = variant_pcm != pcm
                _atomic_wav(path, variant_pcm, metadata)
                support, = time_map_support(input_knots, output_knots)
                observed = np.asarray(measured.get("onsetTimesSeconds", []))
                local = observed[(observed >= a) & (observed <= b)]
                add(name, path, label="defect" if waveform_changed else "uncertain", region=support, transform={"kind": "controlled_time_warp",
                    "timeMethod": time_method, "pattern": kind, "maximumShiftMilliseconds": strength, "regionSeconds": [a, b],
                    "authoredRegionSeconds": [a, b], "timeMapSupportSeconds": [list(support)],
                    "truthRegionBasis": "nonidentity_time_map_segments_and_adjacent_identity_knots", "flankContext": context,
                    "inputTimeKnotsSeconds": input_knots.tolist(), "outputTimeKnotsSeconds": output_knots.tolist(),
                    "measuredSourceOnsetMapping": [[float(t), float(np.interp(t, input_knots, output_knots))] for t in local],
                    "outsideRegionPcmUnchanged": True, "waveformChanged": waveform_changed})
    # Controls compare detector outputs with the baseline; they do not turn its
    # unknown natural quality into clean/intentional ground truth.
    samples = _decode(pcm, width, channels)
    gain = output / "uniform-gain.wav"
    _atomic_wav(gain, _encode(samples * 0.7, width), metadata)
    add("uniform-gain", gain, transform={"kind": "uniform_gain", "gain": 0.7,
                                          "metamorphicBaselineCaseId": f"{source_family}-baseline", "timingMap": "identity"})
    fade_context = _context(measured, metadata["durationSeconds"], max(durations_seconds))
    a, b = fade_context["startSeconds"], fade_context["endSeconds"]
    first, stop = round(a * rate), round(b * rate)
    envelope = 1 - 0.55 * np.sin(np.linspace(0, np.pi, stop - first)) ** 2
    stride = channels * width
    fade_pcm = pcm[:first * stride] + _encode(samples[first:stop] * envelope[:, None], width) + pcm[stop * stride:]
    fade = output / "smooth-fade.wav"
    _atomic_wav(fade, fade_pcm, metadata)
    add("smooth-fade", fade, transform={"kind": "smooth_fade", "minimumGain": 0.45, "regionSeconds": [a, b],
                                       "metamorphicBaselineCaseId": f"{source_family}-baseline", "timingMap": "identity"})
    del samples
    smooth_source, smooth_target = timing_mapping(a, b - a, 200, kind="smooth_tempo")
    smooth = output / "smooth-tempo.wav"
    smooth_pcm = apply_time_map(pcm, metadata, smooth_source, smooth_target)
    smooth_changed = smooth_pcm != pcm
    smooth_support, = time_map_support(smooth_source, smooth_target)
    _atomic_wav(smooth, smooth_pcm, metadata)
    smooth_hash = sha256_file(smooth)
    intent = output / "smooth-tempo-intent.json"
    atomic_write_json(intent, {"version": "rhythm-intent-v1", "sourceArtifactSha256": smooth_hash,
                              "sections": [{"startSeconds": a, "endSeconds": b, "timingIntent": "tempo_change"}]})
    for declaration in (False, True):
        name = "smooth-tempo-with-plan" if declaration else "smooth-tempo-no-plan"
        add(name, smooth, label="intentional" if smooth_changed else "uncertain", region=smooth_support, intent=intent if declaration else None,
            transform={"kind": "deliberate_smooth_time_warp", "timeMethod": time_method, "regionSeconds": [a, b], "authoredRegionSeconds": [a, b],
                       "timeMapSupportSeconds": [list(smooth_support)],
                       "truthRegionBasis": "nonidentity_time_map_segments_and_adjacent_identity_knots", "flankContext": fade_context,
                       "inputTimeKnotsSeconds": smooth_source.tolist(), "outputTimeKnotsSeconds": smooth_target.tolist(),
                       "sameAudioDeclarationPair": [f"{source_family}-smooth-tempo-no-plan", f"{source_family}-smooth-tempo-with-plan"],
                       "explicitPlanAttached": declaration, "outsideRegionPcmUnchanged": True, "waveformChanged": smooth_changed})
    # These are known full-mix digital interruptions. They provide no evidence
    # about a separated accompaniment-only dropout while vocals continue.
    for gap_seconds in (0.10, 0.25, 0.75, 1.5):
        first = round((metadata["durationSeconds"] - gap_seconds) * rate / 2)
        gap_frames = round(gap_seconds * rate)
        stop = first + gap_frames
        a, b = first / rate, stop / rate
        zeros = (b"\x80" if width == 1 else b"\0") * (gap_frames * stride)
        gap_pcm = pcm[:first * stride] + zeros + pcm[stop * stride:]
        gap_name = f"mix-gap-{gap_seconds:g}s"
        gap_path = output / f"{gap_name}.wav"
        _atomic_wav(gap_path, gap_pcm, metadata)
        gap_intent = output / f"{gap_name}-intent.json"
        atomic_write_json(gap_intent, {"version": "rhythm-intent-v1", "sourceArtifactSha256": sha256_file(gap_path),
            "sections": [{"startSeconds": a, "endSeconds": b, "expectedRest": True}]})
        for declaration in (False, True):
            name = gap_name + ("-declared-rest" if declaration else "-defect")
            add(name, gap_path, label=("intentional" if declaration else "defect") if gap_pcm != pcm else "uncertain",
                region=(a, b), category="mix_dropout", review_margin=0.05, intent=gap_intent if declaration else None,
                transform={"kind": "full_mix_digital_gap", "regionSeconds": [a, b], "gapFrames": gap_frames,
                           "exactSampleRange": [first, stop], "outsideRegionPcmUnchanged": True,
                           "waveformChanged": gap_pcm != pcm, "explicitPlanAttached": declaration,
                           "sameAudioDeclarationPair": [f"{source_family}-{gap_name}-defect", f"{source_family}-{gap_name}-declared-rest"],
                           "semanticScope": "Known full-mix interruption, not separated backing dropout."})
    if sha256_file(source) != source_hash:
        raise ValueError("Source changed while building controlled corpus")
    provenance = {"version": BUILDER_VERSION, "createdAt": utc_now(), "sourcePath": str(source),
                  "sourceArtifactSha256": source_hash, "sourceBytes": source.stat().st_size,
                  "sourceFamily": source_family, "split": split, "sourceProject": project_provenance,
                  "clip": metadata, "baselineArtifactSha256": baseline_hash,
                  "measuredSourcePulse": measured.get("pulse"), "measuredSourceKind": measured.get("source", {}).get("kind"),
                  "timeMethod": time_method, "timeMethodMeaning": TIME_METHODS[time_method],
                  "newEngineGeneration": False, "newModelInference": False, "threadLimits": 1, "transforms": transforms,
                  "limits": ["Natural source quality is unknown; baseline, gain and fade are unlabelled.",
                             "Labels describe known imposed timing only, not natural model-error accuracy or human musical intent.",
                             "A coincident baseline defect can contaminate a transform-region alert; paired baseline comparisons are required for causal interpretation.",
                             "Resampling also changes local pitch/timbre; overlap-add keeps pitch but can smear or repeat an attack within its 10 ms tolerance. Both are time-map stress tests, not isolated drum edits.",
                             "Onset mappings document measured source peaks under the intervention; they are never passed to diagnosis as features.",
                             "Insufficient measured pulse context must remain unknown/missed; no requested BPM is substituted.",
                             "Full-mix transformations do not establish separated-backing dropout labels.",
                             "Source family and parent audio must remain entirely in one split."]}
    atomic_write_json(output / "provenance.json", provenance)
    manifest = {"version": VERSION, "cases": cases}
    atomic_write_json(output / "corpus.json", manifest)
    return {"manifestPath": str(output / "corpus.json"), "provenancePath": str(output / "provenance.json"),
            "caseCount": len(cases), "sourceFamily": source_family, "split": split, "sourcePreserved": True}


def _new_json(path: Path, value: Any) -> None:
    """Fsync and atomically publish a revision without replacing any artifact."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _verified_file(path: Path, expected_bytes: Any, expected_hash: Any) -> dict[str, Any]:
    if (isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes <= 0
            or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise ValueError("Explicit reuse requires recorded file size and SHA-256")
    if not path.is_file() or path.stat().st_size != expected_bytes:
        raise ValueError(f"Reuse artifact is missing or its size changed: {path}")
    if sha256_file(path) != expected_hash:
        raise ValueError(f"Reuse artifact SHA-256 changed: {path}")
    return {"path": str(path), "bytes": expected_bytes, "sha256": expected_hash,
            "verification": "file_exists_bytes_sha256"}


def refine_existing_corpus(manifest: Path | str, provenance: Path | str,
                           new_manifest: Path | str, new_provenance: Path | str) -> dict[str, Any]:
    """Revise map-derived labels while explicitly reusing verified existing WAVs.

    Reads authored provenance and artifact bytes only. No detector report,
    prediction, new audio, model, or listening label participates in boundaries.
    Existing manifest, provenance, WAVs, intent plans, and reports remain intact.
    """
    previous_manifest, previous_provenance, manifest_path, provenance_path = [
        Path(path).expanduser().resolve() for path in (manifest, provenance, new_manifest, new_provenance)]
    if manifest_path == provenance_path:
        raise ValueError("Manifest and provenance revision destinations must differ")
    for path in (manifest_path, provenance_path):
        if path.exists():
            raise FileExistsError(path)
    manifest_record = {"path": str(previous_manifest), "bytes": previous_manifest.stat().st_size,
                       "sha256": sha256_file(previous_manifest)}
    provenance_record = {"path": str(previous_provenance), "bytes": previous_provenance.stat().st_size,
                         "sha256": sha256_file(previous_provenance)}
    revised = json.loads(previous_manifest.read_text(encoding="utf-8"))
    validate_manifest(revised)
    source = json.loads(previous_provenance.read_text(encoding="utf-8"))
    if not isinstance(source, dict) or source.get("version") not in ("controlled-rhythm-corpus-v1", BUILDER_VERSION):
        raise ValueError("Refinement requires controlled corpus builder provenance")
    transforms = source.get("transforms")
    if (not isinstance(transforms, list) or len(transforms) != len(revised["cases"])
            or any(not isinstance(item, dict) or not isinstance(item.get("caseId"), str) for item in transforms)):
        raise ValueError("Every corpus case requires exactly one authored transform")
    by_case = {item["caseId"]: item for item in transforms}
    if len(by_case) != len(transforms) or set(by_case) != {case["caseId"] for case in revised["cases"]}:
        raise ValueError("Corpus case IDs and authored transforms disagree")
    reused = [_verified_file(Path(source["sourcePath"]).expanduser().resolve(), source.get("sourceBytes"),
                             source.get("sourceArtifactSha256"))]
    verified_paths: dict[str, tuple[int, str]] = {reused[0]["path"]: (reused[0]["bytes"], reused[0]["sha256"])}
    changed: list[dict[str, Any]] = []
    for case in revised["cases"]:
        transform = by_case[case["caseId"]]
        if (case["labelOrigin"] != "controlled_injection"
                or case.get("parentArtifactSha256") != source["sourceArtifactSha256"]
                or transform.get("parentArtifactSha256") != source["sourceArtifactSha256"]
                or transform.get("sourceArtifactSha256") != case["sourceArtifactSha256"]
                or transform.get("audioPath") != case.get("audioPath")):
            raise ValueError("Corpus case is not bound to its authored transform and source")
        audio_path = Path(case["audioPath"]).expanduser().resolve()
        expected = (case.get("audioBytes"), case["sourceArtifactSha256"])
        if str(audio_path) in verified_paths:
            if verified_paths[str(audio_path)] != expected:
                raise ValueError("Cases disagree about reused audio size or SHA-256")
        else:
            reused.append(_verified_file(audio_path, *expected))
            verified_paths[str(audio_path)] = expected
        if transform.get("kind") in ("controlled_time_warp", "deliberate_smooth_time_warp"):
            support = time_map_support(transform["inputTimeKnotsSeconds"], transform["outputTimeKnotsSeconds"])
            authored = transform["regionSeconds"]
            if (len(support) != 1 or not isinstance(authored, list) or len(authored) != 2
                    or transform["inputTimeKnotsSeconds"][0] != authored[0]
                    or transform["inputTimeKnotsSeconds"][-1] != authored[1]):
                raise ValueError("Refinement requires one connected support inside the authored region")
            a, b = support[0]
            annotations = [item for item in case["annotations"] if item["category"] == "beat_timing"]
            regions = [item for item in case["reviewedRanges"] if item["categories"] == ["beat_timing"]]
            if len(annotations) != 1 or len(regions) != 1:
                raise ValueError("Authored timing transformation requires one timing annotation and reviewed range")
            annotation, region = annotations[0], regions[0]
            before = [annotation["startSeconds"], annotation["endSeconds"]]
            if before != [a, b] or [region["startSeconds"], region["endSeconds"]] != [a, b]:
                changed.append({"caseId": case["caseId"], "previousAnnotationRegionSeconds": before,
                                "previousReviewedRegionSeconds": [region["startSeconds"], region["endSeconds"]],
                                "timeMapSupportSeconds": [a, b]})
            annotation.update(startSeconds=a, endSeconds=b)
            region.update(startSeconds=a, endSeconds=b)
            transform["authoredRegionSeconds"] = list(authored)
            transform["timeMapSupportSeconds"] = [list(support[0])]
            transform["truthRegionBasis"] = "nonidentity_time_map_segments_and_adjacent_identity_knots"
        for annotation in case["annotations"]:
            annotation["evidence"]["sourceRef"] = str(provenance_path) + "#" + case["caseId"]
    validate_manifest(revised)
    _verified_file(previous_manifest, manifest_record["bytes"], manifest_record["sha256"])
    _verified_file(previous_provenance, provenance_record["bytes"], provenance_record["sha256"])
    source["version"] = BUILDER_VERSION
    source["createdAt"] = utc_now()
    source["revision"] = {"kind": "time_map_truth_geometry_refinement",
        "reason": "Label only nonidentity time-map support; broad editing bounds include unchanged burst flanks.",
        "boundarySource": "authored_input_output_time_knots_only", "detectorOutputsUsed": False,
        "previousManifest": manifest_record, "previousProvenance": provenance_record,
        "sourceArtifactSha256": source["sourceArtifactSha256"], "explicitExistingAudioReuse": True,
        "reusedArtifacts": reused, "changedCases": changed}
    for path in (manifest_path, provenance_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    _new_json(provenance_path, source)
    _new_json(manifest_path, revised)
    return {"manifestPath": str(manifest_path), "provenancePath": str(provenance_path),
            "caseCount": len(revised["cases"]), "changedCaseCount": len(changed),
            "verifiedReusedArtifactCount": len(reused), "newAudioFiles": 0, "sourcePreserved": True}


def _csv_numbers(value: str) -> tuple[float, ...]:
    try:
        return tuple(float(part) for part in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use comma-separated numbers") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--split", choices=("calibration", "holdout"))
    parser.add_argument("--source-family")
    parser.add_argument("--refine-existing", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--new-manifest", type=Path)
    parser.add_argument("--new-provenance", type=Path)
    parser.add_argument("--source-project", type=Path)
    parser.add_argument("--start-seconds", type=float, default=0)
    parser.add_argument("--clip-seconds", "--duration-seconds", dest="clip_seconds", type=float, default=60)
    parser.add_argument("--strengths-ms", type=_csv_numbers, default=(50, 90, 130))
    parser.add_argument("--durations-seconds", type=_csv_numbers, default=(4, 6))
    parser.add_argument("--kinds", default="alternating,burst")
    parser.add_argument("--time-method", choices=tuple(TIME_METHODS), default="resample",
                        help="resample shifts pitch with time; overlap_add moves timing only")
    args = parser.parse_args()
    if args.refine_existing:
        if not all((args.manifest, args.provenance, args.new_manifest, args.new_provenance)):
            parser.error("--refine-existing requires --manifest, --provenance, --new-manifest and --new-provenance")
        if any((args.source, args.output_dir, args.split, args.source_family, args.source_project)):
            parser.error("Existing-corpus refinement does not accept new-corpus source or output options")
        result = refine_existing_corpus(args.manifest, args.provenance, args.new_manifest, args.new_provenance)
    else:
        if not all((args.source, args.output_dir, args.split, args.source_family)):
            parser.error("New corpus requires --source, --output-dir, --split and --source-family")
        if any((args.manifest, args.provenance, args.new_manifest, args.new_provenance)):
            parser.error("Revision paths require --refine-existing")
        result = build_corpus(args.source, args.output_dir, split=args.split, source_family=args.source_family,
            source_project=args.source_project, start_seconds=args.start_seconds, clip_seconds=args.clip_seconds,
            strengths_ms=args.strengths_ms, durations_seconds=args.durations_seconds, kinds=tuple(args.kinds.split(",")),
            time_method=args.time_method)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
