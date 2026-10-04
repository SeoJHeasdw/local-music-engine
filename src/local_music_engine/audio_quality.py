"""Streaming, objective PCM checks and optional conservative output finishing.

These measurements cannot judge melody, phrasing, genre, vocal intelligibility or
musical quality.  In particular, rests, quiet passages and isolated full-scale
peaks are never grounds for automatic regeneration.  Original audio is not edited.
"""

from __future__ import annotations

import math
import hashlib
import os
import sys
import tempfile
import warnings
import wave
from array import array
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop


class AudioQualityError(ValueError):
    """An audio file or requested processing policy is invalid."""


@dataclass(frozen=True)
class AudioQualityPolicy:
    window_seconds: float = 0.05
    near_full_scale: float = 0.999
    clipping_run_seconds: float = 0.002
    clipping_sample_fraction: float = 0.001
    digital_silence_amplitude: float = 0.0001
    dc_warning_amplitude: float = 0.01
    boundary_warning_amplitude: float = 0.02
    stereo_correlation_warning: float = -0.95
    stereo_mono_ratio_warning: float = 0.15
    stereo_warning_active_fraction: float = 0.5
    active_window_rms: float = 0.001
    duration_warning_seconds: float = 0.5
    duration_warning_fraction: float = 0.05
    duration_retry_seconds: float = 2.0
    duration_retry_fraction: float = 0.25

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not math.isfinite(value):
                raise AudioQualityError(f"{name} must be finite")
        for name in ("window_seconds", "clipping_run_seconds", "duration_warning_seconds", "duration_retry_seconds"):
            if getattr(self, name) <= 0:
                raise AudioQualityError(f"{name} must be positive")
        if not 0.001 <= self.window_seconds <= 1:
            raise AudioQualityError("window_seconds must be between 0.001 and 1")
        for name in ("near_full_scale", "clipping_sample_fraction", "digital_silence_amplitude", "dc_warning_amplitude", "boundary_warning_amplitude", "stereo_mono_ratio_warning", "stereo_warning_active_fraction", "active_window_rms", "duration_warning_fraction", "duration_retry_fraction"):
            if not 0 < getattr(self, name) <= 1:
                raise AudioQualityError(f"{name} must be in (0, 1]")
        if not -1 <= self.stereo_correlation_warning < 0:
            raise AudioQualityError("stereo_correlation_warning must be in [-1, 0)")
        if self.duration_retry_seconds < self.duration_warning_seconds or self.duration_retry_fraction < self.duration_warning_fraction:
            raise AudioQualityError("retry duration tolerances cannot be smaller than warning tolerances")


@dataclass(frozen=True)
class AudioFinishPolicy:
    """Never boost loudness, compress dynamics, or trim musical content."""

    peak_ceiling: float = 0.98
    dc_correction_amplitude: float = 0.002
    edge_fade_seconds: float = 0.005
    edge_fade_amplitude: float = 0.02

    def __post_init__(self) -> None:
        if any(not math.isfinite(value) for value in asdict(self).values()):
            raise AudioQualityError("finishing thresholds must be finite")
        if not 0 < self.peak_ceiling <= 1:
            raise AudioQualityError("peak_ceiling must be in (0, 1]")
        if not 0 < self.dc_correction_amplitude <= 1 or not 0 < self.edge_fade_amplitude <= 1:
            raise AudioQualityError("amplitude thresholds must be in (0, 1]")
        if not 0 <= self.edge_fade_seconds <= 0.01:
            raise AudioQualityError("edge fades must be between 0 and 10 milliseconds")


def _decode(data: bytes, width: int) -> tuple[array, int]:
    # audioop's 24 -> 32 conversion left-shifts by eight bits.  Keep that scale
    # throughout analysis and convert back only when serializing PCM.
    values = array({1: "b", 2: "h", 3: "i", 4: "i"}[width])
    values.frombytes(audioop.bias(data, 1, -128) if width == 1 else audioop.lin2lin(data, 3, 4) if width == 3 else data)
    if sys.byteorder != "little" and values.itemsize > 1:
        values.byteswap()
    return values, 1 << ((32 if width == 3 else width * 8) - 1)


def _encode(values: array, width: int) -> bytes:
    if sys.byteorder != "little" and values.itemsize > 1:
        values.byteswap()
    data = values.tobytes()
    if width == 1:
        return audioop.bias(data, 1, 128)
    return audioop.lin2lin(data, 4, 3) if width == 3 else data


def _metadata(audio: wave.Wave_read) -> tuple[int, int, int, int]:
    channels, width, rate, frames = audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()
    if audio.getcomptype() != "NONE" or width not in {1, 2, 3, 4}:
        raise AudioQualityError("only uncompressed 8/16/24/32-bit PCM WAV is supported")
    if not 1 <= channels <= 32 or not 1 <= rate <= 384_000 or frames <= 0:
        raise AudioQualityError("WAV has invalid or unsupported channel, rate, or frame metadata")
    return channels, width, rate, frames


def _chunks(audio: wave.Wave_read, frames_per_chunk: int, channels: int, width: int) -> Iterator[tuple[array, int]]:
    # A pathological multi-channel header cannot force an unbounded read.
    frames_per_chunk = min(frames_per_chunk, max(1, 65_536 // channels))
    decoded = 0
    while data := audio.readframes(frames_per_chunk):
        if len(data) % (channels * width):
            raise AudioQualityError("WAV PCM ends in an incomplete frame")
        values, _ = _decode(data, width)
        frames = len(values) // channels
        decoded += frames
        yield values, frames
    if decoded != audio.getnframes():
        raise AudioQualityError(f"WAV PCM is truncated: expected {audio.getnframes()} frames, decoded {decoded}")


def _db(amplitude: float) -> float | None:
    return 20 * math.log10(amplitude) if amplitude > 0 else None


def analyze_audio_quality(
    path: Path | str,
    *,
    requested_duration_seconds: float | None = None,
    policy: AudioQualityPolicy | None = None,
) -> dict[str, Any]:
    """Return bounded objective observations; ``passed`` is not human approval.

    Memory depends on channel count and a sub-second tail, never track duration.
    ``technicalScore`` ranks measurable faults only and makes no musical claim.
    """
    policy = policy or AudioQualityPolicy()
    if requested_duration_seconds is not None and (not math.isfinite(requested_duration_seconds) or requested_duration_seconds <= 0):
        raise AudioQualityError("requested duration must be finite and positive")
    try:
        with wave.open(str(path), "rb") as audio:
            channels, width, rate, frames = _metadata(audio)
            maximum = 1 << ((32 if width == 3 else width * 8) - 1)
            windows = max(1, round(rate * policy.window_seconds))
            sums = [0] * channels
            squares = [0] * channels
            minimums = [maximum] * channels
            maximums = [-maximum] * channels
            rails = [0] * channels
            run_lengths = [0] * channels
            run_signs = [0] * channels
            longest_runs = [0] * channels
            previous: list[int | None] = [None] * channels
            first: list[int] | None = None
            last = [0] * channels
            max_delta = 0
            near_silent_samples = 0
            silent_window_frames = 0
            longest_silent_frames = 0
            current_silent_frames = 0
            active_frames = 0
            cancellation_frames = 0
            stereo_cross_sum = 0
            tail: deque[tuple[int, float]] = deque()
            tail_frames = 0
            rail_threshold = maximum * policy.near_full_scale
            silence_threshold = maximum * policy.digital_silence_amplitude
            for values, chunk_frames in _chunks(audio, windows, channels, width):
                chunk_sums = [0] * channels
                chunk_squares = [0] * channels
                chunk_peak = 0
                cross = 0
                if first is None:
                    first = list(values[:channels])
                for index, value in enumerate(values):
                    channel = index % channels
                    magnitude = abs(value)
                    chunk_peak = max(chunk_peak, magnitude)
                    chunk_sums[channel] += value
                    chunk_squares[channel] += value * value
                    minimums[channel] = min(minimums[channel], value)
                    maximums[channel] = max(maximums[channel], value)
                    near_silent_samples += magnitude <= silence_threshold
                    if previous[channel] is not None:
                        max_delta = max(max_delta, abs(value - previous[channel]))
                    previous[channel] = value
                    last[channel] = value
                    sign = (1 if value >= 0 else -1) if magnitude >= rail_threshold else 0
                    if sign:
                        rails[channel] += 1
                        run_lengths[channel] = run_lengths[channel] + 1 if run_signs[channel] == sign else 1
                        longest_runs[channel] = max(longest_runs[channel], run_lengths[channel])
                    else:
                        run_lengths[channel] = 0
                    run_signs[channel] = sign
                    if channels == 2 and channel == 1:
                        cross += values[index - 1] * value
                for channel in range(channels):
                    sums[channel] += chunk_sums[channel]
                    squares[channel] += chunk_squares[channel]
                chunk_energy = sum(chunk_squares) / (channels * maximum * maximum)
                chunk_rms = math.sqrt(chunk_energy / chunk_frames)
                tail.append((chunk_frames, chunk_energy))
                tail_frames += chunk_frames
                while tail and tail_frames - tail[0][0] >= rate * 0.5:
                    tail_frames -= tail.popleft()[0]
                if chunk_peak <= silence_threshold:
                    silent_window_frames += chunk_frames
                    current_silent_frames += chunk_frames
                    longest_silent_frames = max(longest_silent_frames, current_silent_frames)
                else:
                    current_silent_frames = 0
                if chunk_rms >= policy.active_window_rms:
                    active_frames += chunk_frames
                    if channels == 2:
                        variances = [chunk_squares[c] - chunk_sums[c] ** 2 / chunk_frames for c in range(2)]
                        covariance = cross - chunk_sums[0] * chunk_sums[1] / chunk_frames
                        correlation = covariance / math.sqrt(variances[0] * variances[1]) if min(variances) > 0 else 0
                        stereo_energy = (chunk_squares[0] + chunk_squares[1]) / 2
                        mono_energy = max(0, (chunk_squares[0] + chunk_squares[1] + 2 * cross) / 4)
                        ratio = math.sqrt(mono_energy / stereo_energy) if stereo_energy else 1
                        if correlation <= policy.stereo_correlation_warning and ratio <= policy.stereo_mono_ratio_warning:
                            cancellation_frames += chunk_frames
                stereo_cross_sum += cross
    except (OSError, wave.Error, EOFError) as error:
        raise AudioQualityError(f"WAV quality analysis failed: {error}") from error

    duration = frames / rate
    channel_means = [value / (frames * maximum) for value in sums]
    rms = math.sqrt(sum(squares) / (channels * frames)) / maximum
    peak = max(max(abs(low), abs(high)) for low, high in zip(minimums, maximums)) / maximum
    rail_fraction = sum(rails) / (frames * channels)
    longest_rail_seconds = max(longest_runs) / rate
    tail_energy = tail_count = 0.0
    for count, energy in reversed(tail):
        included = min(count, max(0, rate * 0.1 - tail_count))
        tail_energy += energy * included / count
        tail_count += included
        if tail_count >= rate * 0.1:
            break
    mono_ratio: float | None = None
    correlation: float | None = None
    if channels == 2:
        variances = [squares[c] - sums[c] ** 2 / frames for c in range(2)]
        covariance = stereo_cross_sum - sums[0] * sums[1] / frames
        correlation = max(-1.0, min(1.0, covariance / math.sqrt(variances[0] * variances[1]))) if min(variances) > 0 else None
        energy = (squares[0] + squares[1]) / 2
        mono_ratio = math.sqrt(max(0, (squares[0] + squares[1] + 2 * stereo_cross_sum) / 4) / energy) if energy else None
    metrics: dict[str, Any] = {
        "frames": frames, "sampleRate": rate, "channels": channels, "sampleWidthBytes": width,
        "durationSeconds": duration, "peak": peak, "rms": rms, "rmsDbfs": _db(rms),
        "crestFactorDb": 20 * math.log10(peak / rms) if rms and peak else None,
        "nearFullScaleSampleFraction": rail_fraction,
        "longestFullScaleRunSeconds": longest_rail_seconds,
        "channelDcOffsets": channel_means,
        "channelMinimums": [value / maximum for value in minimums],
        "channelMaximums": [value / maximum for value in maximums],
        "nearDigitalSilenceSampleFraction": near_silent_samples / (frames * channels),
        "digitalSilenceWindowFraction": silent_window_frames / frames,
        "longestDigitalSilenceSeconds": longest_silent_frames / rate,
        "firstFrame": [value / maximum for value in first or []],
        "lastFrame": [value / maximum for value in last],
        "tailRms": math.sqrt(tail_energy / tail_count) if tail_count else 0,
        "maximumAdjacentSampleDelta": max_delta / maximum,
        "stereoCorrelation": correlation, "monoToStereoRmsRatio": mono_ratio,
        "stereoCancellationActiveFraction": cancellation_frames / active_frames if active_frames else 0,
        "activeWindowSeconds": active_frames / rate,
        "analysisWindowSeconds": min(windows, max(1, 65_536 // channels)) / rate,
    }
    findings: list[dict[str, Any]] = []
    deductions = 0

    def add(check: str, severity: str, message: str, observed: dict[str, Any], threshold: dict[str, Any], *, retry: bool = False, penalty: int = 0) -> None:
        nonlocal deductions
        deductions += penalty
        findings.append({"check": check, "severity": severity, "message": message, "observed": observed, "threshold": threshold, "retryEligible": retry, "confidence": 1.0, "startSeconds": None, "endSeconds": None})

    silent = peak <= policy.digital_silence_amplitude
    if silent:
        add("digital_silence", "error", "The entire output is at or below near-digital silence.", {"peak": peak}, {"maximumAmplitude": policy.digital_silence_amplitude}, retry=True, penalty=100)
    sustained = longest_rail_seconds >= policy.clipping_run_seconds and max(longest_runs) >= 3 and rail_fraction >= policy.clipping_sample_fraction
    if sustained:
        add("sustained_clipping", "error", "Repeated same-polarity samples remain at full scale; lowering gain cannot restore the lost waveform.", {"sampleFraction": rail_fraction, "longestRunSeconds": longest_rail_seconds}, {"minimumRunSeconds": policy.clipping_run_seconds, "minimumSampleFraction": policy.clipping_sample_fraction, "amplitudeAtOrAbove": policy.near_full_scale}, retry=True, penalty=60)
    elif peak >= policy.near_full_scale:
        add("full_scale_peak", "info", "Full-scale peaks were observed without sustained clipping evidence.", {"peak": peak, "longestRunSeconds": longest_rail_seconds}, {"sustainedRunSeconds": policy.clipping_run_seconds})
    dc = max(abs(value) for value in channel_means)
    if dc > policy.dc_warning_amplitude:
        add("dc_offset", "warning", "A measurable DC offset reduces available headroom; a separate finished copy can remove it.", {"channelDcOffsets": channel_means}, {"warningAbsoluteOffsetAbove": policy.dc_warning_amplitude}, penalty=5)
    for name, edge in (("start", metrics["firstFrame"]), ("end", metrics["lastFrame"])):
        amplitude = max(abs(value) for value in edge)
        if amplitude >= policy.boundary_warning_amplitude:
            add(f"abrupt_{name}", "warning", "The waveform meets the file boundary away from zero; this is a possible click, not a judgment of musical phrasing.", {"boundaryAmplitude": amplitude}, {"warningAmplitudeAtOrAbove": policy.boundary_warning_amplitude}, penalty=3)
            findings[-1].update({"startSeconds": 0 if name == "start" else max(0, duration - 0.01), "endSeconds": min(duration, 0.01) if name == "start" else duration})
    cancellation_fraction = metrics["stereoCancellationActiveFraction"]
    if channels == 2 and active_frames >= min(frames, rate * 0.5) and cancellation_fraction >= policy.stereo_warning_active_fraction:
        add("stereo_mono_cancellation", "warning", "The active stereo mix substantially cancels in mono; intentional spatial effects can also cause this observation.", {"activeFraction": cancellation_fraction, "correlation": correlation, "monoToStereoRmsRatio": mono_ratio}, {"correlationAtOrBelow": policy.stereo_correlation_warning, "monoRatioAtOrBelow": policy.stereo_mono_ratio_warning, "minimumActiveFraction": policy.stereo_warning_active_fraction}, penalty=15)
    if requested_duration_seconds is not None:
        difference = abs(duration - requested_duration_seconds)
        warning = max(policy.duration_warning_seconds, requested_duration_seconds * policy.duration_warning_fraction)
        retry_threshold = max(policy.duration_retry_seconds, requested_duration_seconds * policy.duration_retry_fraction)
        metrics["requestedDurationSeconds"] = requested_duration_seconds
        metrics["durationDifferenceSeconds"] = difference
        if difference > warning:
            retry = difference > retry_threshold
            add("duration_mismatch", "error" if retry else "warning", "Decoded duration differs substantially from the requested duration.", {"actualSeconds": duration, "requestedSeconds": requested_duration_seconds, "differenceSeconds": difference}, {"warningDifferenceAboveSeconds": warning, "retryDifferenceAboveSeconds": retry_threshold}, retry=retry, penalty=40 if retry else 5)
    retry = any(finding["retryEligible"] for finding in findings)
    return {
        "version": 1, "metrics": metrics, "findings": findings, "retryEligible": retry,
        "automaticStatus": "retry_recommended" if retry else "needs_review" if any(item["severity"] == "warning" for item in findings) else "passed",
        "technicalScore": max(0, 100 - deductions), "policy": asdict(policy),
    }


def finish_audio(
    source: Path | str,
    destination: Path | str,
    *,
    policy: AudioFinishPolicy | None = None,
    on_staged: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Create an exclusive new PCM WAV with transparent, bounded corrections.

    DC removal uses each channel's measured mean. Gain is never increased. At
    most ten milliseconds per boundary are faded, only where a DC-corrected
    edge is nonzero. There is no content trimming, compression or noise gate.
    Existing clipping remains a defect and must not be marked repaired.

    ``on_staged`` receives the durable temporary file's identity before
    publication. When supplied, its anchor remains for the caller to release
    after manifest commit, or to use for interrupted-job cleanup. The callback
    must persist ownership before returning; it can also inspect/cancel staging.
    """
    policy = policy or AudioFinishPolicy()
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise AudioQualityError("finished audio must use a separate destination")
    if destination.exists():
        raise FileExistsError(destination)
    before = analyze_audio_quality(source)
    metrics = before["metrics"]
    channels, width, rate, frames = (metrics[name] for name in ("channels", "sampleWidthBytes", "sampleRate", "frames"))
    offsets = [value if abs(value) > policy.dc_correction_amplitude else 0 for value in metrics["channelDcOffsets"]]
    corrected_peak = max(max(abs(low - offset), abs(high - offset)) for low, high, offset in zip(metrics["channelMinimums"], metrics["channelMaximums"], offsets))
    gain = min(1.0, policy.peak_ceiling / corrected_peak) if corrected_peak else 1.0
    # Fade no more than half the file per side for tiny diagnostic WAVs.
    fade_frames = min(round(rate * policy.edge_fade_seconds), frames // 2)
    fade_start = fade_frames > 1 and max(abs(value - offset) * gain for value, offset in zip(metrics["firstFrame"], offsets)) >= policy.edge_fade_amplitude
    fade_end = fade_frames > 1 and max(abs(value - offset) * gain for value, offset in zip(metrics["lastFrame"], offsets)) >= policy.edge_fade_amplitude
    changed = any(offsets) or gain < 1.0 or fade_start or fade_end
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    staged_output: dict[str, Any] | None = None
    staged_handoff = False
    completed = False

    def sync_directory() -> None:
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    try:
        with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False) as raw:
            temporary = Path(raw.name)
            with wave.open(str(source), "rb") as input_audio, wave.open(raw, "wb") as output:
                source_metadata = _metadata(input_audio)
                if source_metadata != (channels, width, rate, frames):
                    raise AudioQualityError("source audio metadata changed during finishing")
                output.setparams(input_audio.getparams())
                maximum = 1 << ((32 if width == 3 else width * 8) - 1)
                integer_offsets = [offset * maximum for offset in offsets]
                position = 0
                for values, chunk_frames in _chunks(input_audio, 4096, channels, width):
                    if changed:
                        for index, value in enumerate(values):
                            frame = position + index // channels
                            channel = index % channels
                            envelope = 1.0
                            if fade_start and frame < fade_frames:
                                envelope *= frame / (fade_frames - 1)
                            if fade_end and frame >= frames - fade_frames:
                                envelope *= (frames - 1 - frame) / (fade_frames - 1)
                            corrected = round((value - integer_offsets[channel]) * gain * envelope)
                            # A 24-bit sample occupies the high 24 bits of int32.
                            if width == 3:
                                corrected = round(corrected / 256) * 256
                            values[index] = max(-maximum, min(maximum - (256 if width == 3 else 1), corrected))
                    output.writeframesraw(_encode(values, width))
                    position += chunk_frames
            raw.flush()
            os.fsync(raw.fileno())
        current = temporary.stat()
        with temporary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        staged_output = {
            "path": str(destination.absolute()), "temporaryPath": str(temporary.absolute()),
            "device": current.st_dev, "inode": current.st_ino,
            "expectedBytes": current.st_size, "expectedSha256": digest,
        }
        sync_directory()
        if on_staged is not None:
            staged_handoff = True
            on_staged(dict(staged_output))
        # Publication is atomic and exclusive; a race cannot overwrite another
        # artifact. The temporary and destination are on the same filesystem.
        os.link(temporary, destination)
        sync_directory()
        after = analyze_audio_quality(destination)
        completed = True
    except (wave.Error, EOFError) as error:
        raise AudioQualityError(f"audio finishing failed: {error}") from error
    finally:
        if temporary is not None and not staged_handoff:
            if not completed and staged_output is not None:
                # Standalone callers have no manifest owner. Roll back only our
                # exact published inode, preserving a file someone replaced.
                try:
                    current = destination.lstat()
                    if (current.st_dev, current.st_ino) == (staged_output["device"], staged_output["inode"]):
                        with destination.open("rb") as stream:
                            unchanged = hashlib.file_digest(stream, "sha256").hexdigest() == staged_output["expectedSha256"]
                        if unchanged:
                            destination.unlink()
                except FileNotFoundError:
                    pass
            temporary.unlink(missing_ok=True)
    return {
        "version": 1, "changed": changed,
        "processing": {"channelDcOffsetsRemoved": offsets, "gain": gain, "gainDb": _db(gain), "startFadeSeconds": fade_frames / rate if fade_start else 0, "endFadeSeconds": fade_frames / rate if fade_end else 0, "peakCeiling": policy.peak_ceiling},
        "sourceQuality": before, "outputQuality": after,
        "existingClippingRepaired": False,
        "stagedOutput": staged_output if on_staged is not None else None,
    }
