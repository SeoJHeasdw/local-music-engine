"""Model-free musical timing observations, never an automatic mix correction.

The inferred pulse is not proof of meter, downbeat, groove or musical quality.
Only PCM and short FFT batches are held in memory; compact feature envelopes
are bounded by the same 610-second limit as the optional quality worker.
"""

from __future__ import annotations

import math
import re
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .production_rules import normalize_time_signature

VERSION = "music-structure-v1"
MAX_AUDIO_SECONDS = 610.0
MAX_DISPLAY_POINTS = 2400
MIN_RHYTHM_SECONDS = 6.0
MIN_ONSETS = 8
MIN_CONFIDENCE = 0.55
METRICAL_RATIOS = (0.25, 1 / 3, 0.5, 2 / 3, 1, 1.5, 2, 3, 4)
FRAME_COLUMNS = ["timeSeconds", "rmsDbfs", "peak", "spectralCentroidHz",
                 "lowEnergyFraction", "midEnergyFraction", "highEnergyFraction", "spectralFlux"]


class MusicStructureError(ValueError):
    """Invalid audio or a request outside the bounded analysis contract."""


def _decode(data: bytes, width: int, channels: int) -> np.ndarray:
    if len(data) % (width * channels):
        raise MusicStructureError("WAV PCM ends in an incomplete frame")
    if width == 3:
        octets = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        integers = octets[:, 0] | octets[:, 1] << 8 | octets[:, 2] << 16
        integers = (integers ^ 0x800000) - 0x800000
    else:
        integers = np.frombuffer(data, dtype={1: "u1", 2: "<i2", 4: "<i4"}[width])
        if width == 1:
            integers = integers.astype(np.int16) - 128
    return (integers.astype(np.float64) / (1 << (width * 8 - 1))).reshape(-1, channels)


def _features(path: Path) -> tuple[np.ndarray, float, float, float]:
    try:
        with wave.open(str(path), "rb") as audio:
            channels, width, rate, expected_frames = (audio.getnchannels(), audio.getsampwidth(),
                                                      audio.getframerate(), audio.getnframes())
            if audio.getcomptype() != "NONE" or width not in {1, 2, 3, 4}:
                raise MusicStructureError("only uncompressed 8/16/24/32-bit PCM WAV is supported")
            if not 1 <= channels <= 32 or not 1 <= rate <= 384_000 or expected_frames <= 0:
                raise MusicStructureError("WAV has invalid or unsupported channel, rate, or frame metadata")
            duration = expected_frames / rate
            if duration > MAX_AUDIO_SECONDS:
                raise MusicStructureError(f"structure analysis supports at most {MAX_AUDIO_SECONDS:g} seconds")
            hop = max(1, round(rate * 0.01))
            fft_size = 1 << max(5, min(13, round(math.log2(max(32, rate * 0.046)))))
            window = np.hanning(fft_size)
            frequencies = np.fft.rfftfreq(fft_size, 1 / rate)
            bands = (frequencies < 250, (frequencies >= 250) & (frequencies < 4000), frequencies >= 4000)
            buffer = np.empty((0, channels), dtype=np.float64)
            batches: list[np.ndarray] = []
            previous_magnitude = np.zeros(len(frequencies))
            decoded = position = 0

            def extract(samples: np.ndarray, *, final: bool = False) -> None:
                nonlocal previous_magnitude, position
                if final:
                    frames = np.pad(samples, ((0, max(0, fft_size - len(samples))), (0, 0)))[None, :, :].transpose(0, 2, 1)
                else:
                    frames = np.lib.stride_tricks.sliding_window_view(samples, fft_size, axis=0)[::hop]
                rms = np.sqrt(np.mean(frames * frames, axis=(1, 2)))
                peak = np.max(np.abs(frames), axis=(1, 2))
                # Combine channel power, not signed samples: antiphase stereo
                # still contains music and must retain its onset evidence.
                spectrum = np.fft.rfft(frames * window, axis=-1)
                power = np.mean(np.abs(spectrum) ** 2, axis=1)
                power[:, 0] = 0  # DC is measured separately by integrity QC.
                magnitude = np.sqrt(power)
                denominator = magnitude.sum(axis=1)
                centroid = np.divide(magnitude @ frequencies, denominator,
                                     out=np.zeros(len(frames)), where=denominator > 0)
                power_sum = power.sum(axis=1)
                fractions = [np.divide(power[:, band].sum(axis=1), power_sum,
                                       out=np.zeros(len(frames)), where=power_sum > 0) for band in bands]
                preceding = np.vstack((previous_magnitude, magnitude[:-1]))
                flux = np.divide(np.maximum(0, magnitude - preceding).sum(axis=1), denominator,
                                 out=np.zeros(len(frames)), where=denominator > 0)
                flux[rms < 0.0001] = 0
                previous_magnitude = magnitude[-1]
                db = 20 * np.log10(np.maximum(rms, 1e-12))
                times = (position + np.arange(len(frames)) * hop + min(fft_size, len(samples)) / 2) / rate
                times = np.minimum(times, duration)
                batches.append(np.column_stack((times, db, peak, centroid, *fractions, flux)))
                position += len(frames) * hop

            while data := audio.readframes(min(8192, max(1, 65_536 // channels))):
                samples = _decode(data, width, channels)
                decoded += len(samples)
                buffer = np.concatenate((buffer, samples), axis=0)
                if len(buffer) >= fft_size:
                    count = 1 + (len(buffer) - fft_size) // hop
                    extract(buffer)
                    buffer = buffer[count * hop:]
            if decoded != expected_frames:
                raise MusicStructureError(f"WAV PCM is truncated: expected {expected_frames} frames, decoded {decoded}")
            # The retained overlap can also include up to one new hop at the
            # end of the file. A padded final observation covers those samples
            # instead of dropping a last-sample peak or end transient.
            if len(buffer):
                extract(buffer, final=True)
            return np.concatenate(batches), hop / rate, fft_size / rate, duration
    except (OSError, wave.Error, EOFError) as error:
        raise MusicStructureError(f"WAV structure analysis failed: {error}") from error


def _onsets(flux: np.ndarray, hop: float) -> tuple[np.ndarray, np.ndarray]:
    """Reject weak novelty and sustained tones before examining periodicity."""
    baseline = float(np.median(flux))
    deviation = float(np.median(np.abs(flux - baseline)))
    threshold = max(0.025, baseline + 3 * deviation)
    indices = np.flatnonzero((flux[1:-1] >= flux[:-2]) & (flux[1:-1] > flux[2:]) & (flux[1:-1] >= threshold)) + 1
    selected: list[int] = []
    spacing = max(1, round(0.06 / hop))
    for index in indices:
        if selected and index - selected[-1] < spacing:
            if flux[index] > flux[selected[-1]]:
                selected[-1] = int(index)
        else:
            selected.append(int(index))
    peaks = np.asarray(selected, dtype=np.int64)
    envelope = np.zeros(len(flux))
    envelope[peaks] = flux[peaks] - baseline
    return peaks, envelope


def _tempo(flux: np.ndarray, hop: float) -> dict[str, Any]:
    peaks, envelope = _onsets(flux, hop)
    empty = {"bpm": None, "confidence": 0.0, "candidates": [], "onsetCount": int(len(peaks)), "peaks": peaks}
    if len(flux) * hop < MIN_RHYTHM_SECONDS or len(peaks) < MIN_ONSETS:
        return empty
    # Observed onsets land on a 10 ms frame grid. Blur by two frames before
    # autocorrelation so e.g. a 90 BPM period split between 66 and 67 frames
    # is not weaker than its exactly quantized three-period alias at 200.
    # Removing the mean prevents random onset density looking periodic.
    envelope = np.convolve(envelope, [0.0625, 0.25, 0.375, 0.25, 0.0625], mode="same")
    envelope -= float(np.mean(envelope))
    size = 1 << (2 * len(envelope) - 1).bit_length()
    transformed = np.fft.rfft(envelope, n=size)
    correlations = np.fft.irfft(transformed * transformed.conjugate(), n=size)[:len(envelope)]
    squares = np.concatenate(([0.0], np.cumsum(envelope * envelope)))
    first, last = max(1, round(60 / 300 / hop)), min(len(envelope) - 1, round(60 / 30 / hop))
    lags = np.arange(first, last + 1)
    denominator = np.sqrt((squares[-1] - squares[lags]) * squares[len(envelope) - lags])
    scores = np.divide(correlations[lags], denominator, out=np.zeros(len(lags)), where=denominator > 0)
    # Accommodate onset localization spread, without averaging an entire beat.
    if len(scores) > 2:
        scores = np.maximum(scores, np.maximum(np.r_[0, scores[:-1]], np.r_[scores[1:], 0]) * 0.97)
    maxima = [index for index in range(len(scores))
              if scores[index] >= (scores[index - 1] if index else 0)
              and scores[index] > (scores[index + 1] if index + 1 < len(scores) else 0)]
    maxima.sort(key=lambda index: float(scores[index]), reverse=True)
    candidates: list[dict[str, float]] = []
    for index in maxima:
        bpm = 60 / (float(lags[index]) * hop)
        strength = max(0.0, min(1.0, float(scores[index])))
        if strength < 0.15 or any(abs(bpm - item["bpm"]) / item["bpm"] < 0.025 for item in candidates):
            continue
        candidates.append({"bpm": round(bpm, 3), "strength": round(strength, 4)})
        if len(candidates) == 8:
            break
    if not candidates:
        return empty
    strongest = max(item["strength"] for item in candidates)
    # The fastest strongly supported repetition avoids arbitrarily selecting a
    # multiple-period autocorrelation peak. It is still only a pulse estimate.
    best = max((item for item in candidates if item["strength"] >= strongest * 0.9), key=lambda item: item["bpm"])
    confidence = best["strength"] * min(1.0, len(peaks) / 12)
    if confidence < MIN_CONFIDENCE:
        return {**empty, "confidence": round(confidence, 4), "candidates": candidates}
    return {"bpm": best["bpm"], "confidence": round(confidence, 4), "candidates": candidates,
            "onsetCount": int(len(peaks)), "peaks": peaks}


def _observed_beats(features: np.ndarray, tempo: dict[str, Any]) -> list[float]:
    if tempo["bpm"] is None:
        return []
    period = 60 / tempo["bpm"]
    peaks = tempo["peaks"]
    times = features[peaks, 0]
    weights = features[peaks, 7]
    # Phase is inferred from actual onset observations and estimated tempo,
    # never from the requested BPM. No missing onset is invented as a beat.
    phases = np.linspace(0, period, 100, endpoint=False)
    distance = np.abs((times[:, None] - phases + period / 2) % period - period / 2)
    phase = float(phases[np.argmax((np.maximum(0, 1 - distance / (period * 0.15)) * weights[:, None]).sum(axis=0))])
    chosen: dict[int, tuple[float, float]] = {}
    for time, weight in zip(times, weights):
        index = round((float(time) - phase) / period)
        if abs(float(time) - (phase + index * period)) <= period * 0.18:
            if index not in chosen or weight > chosen[index][1]:
                chosen[index] = (float(time), float(weight))
    return [round(chosen[index][0], 4) for index in sorted(chosen)]


def _beat_intervals(beats: list[float], estimated_bpm: float | None) -> dict[str, Any]:
    metrics: dict[str, Any] = {"status": "unknown", "observedPulseCount": len(beats), "intervalCount": 0,
        "estimatedPeriodSeconds": 60 / estimated_bpm if estimated_bpm else None,
        "medianIntervalSeconds": None, "intervalMadMilliseconds": None,
        "timingDeviationP95Milliseconds": None, "skippedPulseCount": None,
        "method": "observed onset intervals; gaps normalized by the independently estimated pulse period",
        "caveat": "These are inferred pulse candidates, not verified beats; gaps and timing variation can be intentional rests or syncopation."}
    if estimated_bpm is None or len(beats) < 3:
        return metrics
    period = 60 / estimated_bpm
    intervals = np.diff(beats)
    multiples = np.maximum(1, np.rint(intervals / period))
    normalized = intervals / multiples
    median = float(np.median(normalized))
    metrics.update(status="measured", intervalCount=len(intervals),
        estimatedPeriodSeconds=round(period, 6), medianIntervalSeconds=round(median, 6),
        intervalMadMilliseconds=round(float(np.median(np.abs(normalized - median))) * 1000, 3),
        timingDeviationP95Milliseconds=round(float(np.percentile(np.abs(normalized - period), 95)) * 1000, 3),
        skippedPulseCount=int(np.sum(multiples - 1)))
    return metrics


def _frame_series(features: np.ndarray, hop: float, window: float) -> dict[str, Any]:
    stride = max(1, math.ceil(len(features) / MAX_DISPLAY_POINTS))
    points = []
    for start in range(0, len(features), stride):
        block = features[start:start + stride]
        point = np.mean(block, axis=0)
        # Preserve transients in display buckets and average linear energy,
        # rather than dB values. A fully silent bucket is JSON null.
        energy = float(np.mean(10 ** (block[:, 1] / 10)))
        point[1] = 10 * math.log10(energy) if energy > 1e-22 else -240
        point[2] = np.max(block[:, 2])
        point[7] = np.max(block[:, 7])
        row: list[float | None] = [round(float(value), 6) for value in point]
        if energy <= 1e-22:
            row[1] = None
        points.append(row)
    return {"columns": FRAME_COLUMNS, "points": points, "maximumPoints": MAX_DISPLAY_POINTS,
            "analysisHopSeconds": hop, "analysisWindowSeconds": window, "intervalSeconds": hop * stride,
            "aggregation": "mean band fractions and centroid; energy-averaged RMS; maximum peak and flux"}


def _shift_findings(features: np.ndarray, hop: float, duration: float) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    flank = max(1, round(0.5 / hop))
    step = max(1, round(0.1 / hop))
    candidates: dict[str, list[tuple[float, float]]] = {"loudness_shift": [], "spectral_shift": []}
    for index in range(2 * flank, len(features) - 2 * flank, step):
        before, after = features[index - flank:index], features[index:index + flank]
        left_db, right_db = float(np.median(before[:, 1])), float(np.median(after[:, 1]))
        if min(left_db, right_db) < -60:
            continue
        loudness_delta = abs(right_db - left_db)
        spectrum_delta = float(np.abs(np.median(before[:, 4:7], axis=0) - np.median(after[:, 4:7], axis=0)).sum())
        time = float(features[index, 0])
        if loudness_delta >= 9:
            candidates["loudness_shift"].append((time, loudness_delta))
        if spectrum_delta >= 1.2:
            candidates["spectral_shift"].append((time, spectrum_delta))
    for check, observations in candidates.items():
        kept: list[tuple[float, float]] = []
        for time, value in sorted(observations, key=lambda item: item[1], reverse=True):
            if all(abs(time - existing[0]) > 1.0 for existing in kept):
                kept.append((time, value))
            if len(kept) >= 12:
                break
        for time, value in sorted(kept):
            findings.append({"check": check, "severity": "warning", "retryEligible": False,
                "confidence": 1.0, "startSeconds": round(max(0, time - 0.5), 4),
                "endSeconds": round(min(duration, time + 0.5), 4), "value": round(value, 4),
                "observed": {"differenceDb" if check == "loudness_shift" else "bandFractionDistance": round(value, 4)},
                "threshold": {"warningDifferenceDb": 9} if check == "loudness_shift" else {"warningBandFractionDistance": 1.2},
                "message": ("The local RMS level changes abruptly; listen to decide whether this is an intentional arrangement change."
                            if check == "loudness_shift" else
                            "The local spectral balance changes abruptly; listen to decide whether this is an intentional instrument or section change.")})
    return findings


def analyze_music_structure(path: Path | str, *, requested_bpm: float | None = None,
                            time_signature: str | int | None = None, style_prompt: str = "") -> dict[str, Any]:
    """Observe generated audio independently of its requested timing.

    Warnings are listening aids and are never eligible for automatic retry or
    destructive correction. Requested meter only projects bar length; there
    is no downbeat or meter verifier in this model-free analyzer.
    """
    if requested_bpm is not None and (isinstance(requested_bpm, bool) or not isinstance(requested_bpm, (int, float))
                                      or not math.isfinite(requested_bpm) or not 30 <= requested_bpm <= 300):
        raise MusicStructureError("requested BPM must be finite and between 30 and 300")
    try:
        meter_value = normalize_time_signature(time_signature)
    except ValueError as error:
        raise MusicStructureError(str(error)) from error
    features, hop, window, duration = _features(Path(path))
    tempo = _tempo(features[:, 7], hop)
    segments: list[dict[str, Any]] = []
    segment_seconds = max(8.0, min(16.0, 16 * 60 / tempo["bpm"])) if tempo["bpm"] else 12.0
    segment_frames = max(1, round(segment_seconds / hop))
    for start in range(0, len(features), segment_frames):
        local = features[start:start + segment_frames]
        if len(local) * hop < MIN_RHYTHM_SECONDS:
            continue
        estimate = _tempo(local[:, 7], hop)
        segments.append({"startSeconds": round(max(0, float(local[0, 0]) - window / 2), 4),
                         "endSeconds": round(min(duration, float(local[-1, 0]) + hop), 4),
                         "estimatedBpm": estimate["bpm"], "confidence": estimate["confidence"],
                         "onsetCount": estimate["onsetCount"]})
    findings = _shift_findings(features, hop, duration)

    def add(check: str, message: str, observed: dict[str, Any], threshold: dict[str, Any], *, severity: str = "warning", region=None,
            confidence: float | None = None) -> None:
        findings.append({"check": check, "severity": severity, "message": message, "observed": observed,
                         "threshold": threshold, "confidence": tempo["confidence"] if confidence is None else confidence, "retryEligible": False,
                         "startSeconds": region[0] if region else None, "endSeconds": region[1] if region else None,
                         "value": observed.get("differencePercent", observed.get("rangePercent"))})

    supported = [item for item in segments if item["estimatedBpm"] is not None and item["confidence"] >= MIN_CONFIDENCE]
    aligned = []
    reference_bpm = tempo["bpm"] or (float(np.median([item["estimatedBpm"] for item in supported])) if supported else None)
    if reference_bpm is not None:
        for item in supported:
            local = item["estimatedBpm"]
            related = min((local * ratio for ratio in METRICAL_RATIOS), key=lambda bpm: abs(math.log(bpm / reference_bpm)))
            # Only fold close metrical aliases. Choosing whichever ratio is
            # merely nearer would conceal genuine changes such as120→150.
            comparison = related if abs(related - reference_bpm) / reference_bpm <= 0.05 else local
            item["metricalComparisonBpm"] = round(comparison, 3)
            aligned.append(comparison)
    stable = not aligned or (max(aligned) - min(aligned)) / float(np.median(aligned)) <= 0.08
    varying_intent = bool(re.search(r"rubato|accelerando|ritardando|tempo\s*chang|free\s*time|변박|템포\s*변|박자\s*변|루바토|자유로운\s*박", style_prompt, re.I))
    if requested_bpm is not None and tempo["bpm"] is not None and stable:
        difference = abs(tempo["bpm"] - requested_bpm) / requested_bpm
        metrical_difference = min(abs(tempo["bpm"] * ratio - requested_bpm) / requested_bpm for ratio in METRICAL_RATIOS)
        if difference > 0.05 and metrical_difference <= 0.05:
            add("tempo_ambiguity", "The observed repetition is compatible with a subdivision, compound beat or bar rate of the requested BPM; metrical level cannot be verified from these onset observations.",
                {"estimatedBpm": tempo["bpm"], "requestedBpm": requested_bpm, "differencePercent": round(difference * 100, 2)},
                {"metricalRatioTolerancePercent": 5})
        elif difference > 0.05 and abs(tempo["bpm"] - requested_bpm) > 3:
            add("tempo_mismatch", "The confidently observed repeating pulse differs from the requested BPM; listen to confirm the intended groove and beat unit.",
                {"estimatedBpm": tempo["bpm"], "requestedBpm": requested_bpm, "differencePercent": round(difference * 100, 2)},
                {"warningDifferencePercent": 5, "warningDifferenceBpm": 3}, severity="info" if varying_intent else "warning")
    if len(aligned) >= 2:
        spread = (max(aligned) - min(aligned)) / float(np.median(aligned))
        if spread > 0.08 and max(aligned) - min(aligned) > 4:
            add("tempo_drift", "Local pulse estimates vary across the track; intentional tempo changes and rhythmic subdivisions can also explain this observation.",
                {"rangePercent": round(spread * 100, 2), "minimumBpm": round(min(aligned), 3), "maximumBpm": round(max(aligned), 3)},
                {"warningRangePercent": 8, "warningRangeBpm": 4}, severity="info" if varying_intent else "warning",
                region=(supported[0]["startSeconds"], supported[-1]["endSeconds"]),
                confidence=min(item["confidence"] for item in supported))
    quarter_notes = {"2": 2, "3": 3, "4": 4, "6": 3}.get(meter_value)
    # An inferred pulse can be a subdivision, compound beat or whole bar.
    # Only the explicitly requested quarter-note BPM can project bar length.
    projected_bpm = requested_bpm
    meter = {"status": "projected" if quarter_notes is not None and projected_bpm is not None else "unknown",
             "timeSignature": {"2": "2/4", "3": "3/4", "4": "4/4", "6": "6/8"}.get(meter_value),
             "quarterNotesPerBar": quarter_notes, "bpmBeatUnit": "quarter-note",
             "projectedBarDurationSeconds": round(quarter_notes * 60 / projected_bpm, 6) if quarter_notes and projected_bpm else None,
             "caveat": "Requested meter projects bar length only; no downbeat or actual meter has been verified."}
    status = "needs_review" if any(item["severity"] == "warning" for item in findings) else "observed" if tempo["bpm"] is not None else "unknown"
    beats = _observed_beats(features, tempo)
    return {"version": VERSION, "status": status, "requestedBpm": requested_bpm, "estimatedBpm": tempo["bpm"],
            "confidence": tempo["confidence"], "tempoCandidates": tempo["candidates"], "segments": segments,
            "confidenceMeaning": "strength of periodic onset repetition, not probability that the pulse is the musical quarter-note beat",
            "beatTimesSeconds": beats, "beatTimesMethod": "observed onset peaks near independently estimated pulse phase",
            "beatIntervalMetrics": _beat_intervals(beats, tempo["bpm"]),
            "meter": meter, "frameSeries": _frame_series(features, hop, window), "findings": findings,
            "durationSeconds": duration, "onsetCount": tempo["onsetCount"],
            "limitations": ["Pulse estimates can be ambiguous at subdivisions, compound beats or bar-level repetitions.",
                            "Onsets do not verify downbeats, meter, swing, syncopation or musical quality.",
                            "Frame RMS is dBFS, not perceptual loudness or LUFS; spectral bands are normalized power fractions."]}
