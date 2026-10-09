"""Read-only timing evidence from a median-HPSS percussive spectral proxy.

HPSS distinguishes transient and sustained spectral structure. It does not
identify instruments, separate vocals from backing, or produce semantic drum
stems. Only compact feature envelopes span the full audio file; PCM and STFT
matrices are bounded to two-second cores with half-second context on each side.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import median_filter

from .music_structure import (
    MAX_AUDIO_SECONDS,
    MAX_DISPLAY_POINTS,
    MIN_CONFIDENCE,
    MIN_RHYTHM_SECONDS,
    MusicStructureError,
    _decode,
    _observed_beats,
    _onsets,
    _tempo,
)

VERSION = "percussion-timing-v2"
CORE_SECONDS = 2.0
CONTEXT_SECONDS = 0.5
FRAME_COLUMNS = ["timeSeconds", "rmsDbfs", "magnitudeFlux", "harmonicRms", "percussiveRms"]
MIN_CANDIDATE_CONFIDENCE = 0.6


def _supported_beat_candidates(features: np.ndarray, tempo: dict[str, Any],
                               segments: list[dict[str, Any]]) -> list[float]:
    """Select measured onsets using a supported global or adjacent local pulse.

    A weak whole-track estimate must not phase-filter every locally supported
    region using its possibly different metrical alias. Local phase is fitted
    jointly within each continuous group, without extrapolating into the rest
    of the source or creating an onset where none was measured.
    """
    if tempo["bpm"] is not None and tempo["confidence"] >= MIN_CANDIDATE_CONFIDENCE:
        return _observed_beats(features, tempo)
    groups: list[list[dict[str, Any]]] = []
    for segment in segments:
        if segment["bpm"] is None or segment["confidence"] < MIN_CANDIDATE_CONFIDENCE:
            continue
        if (groups and abs(segment["startSeconds"] - groups[-1][-1]["endSeconds"]) <= 0.0001
                and abs(segment["bpm"] / groups[-1][-1]["bpm"] - 1) <= 0.05
                and abs(segment["bpm"] / groups[-1][0]["bpm"] - 1) <= 0.05):
            groups[-1].append(segment)
        else:
            groups.append([segment])
    peaks = tempo["peaks"]
    times = features[peaks, 0]
    candidates: set[float] = set()
    for group in groups:
        # One isolated autocorrelation estimate cannot establish a local track.
        if len(group) < 2:
            continue
        start, end = group[0]["startSeconds"], group[-1]["endSeconds"]
        observed = peaks[(times >= start) & (times < end)]
        if not len(observed):
            continue
        local = {"bpm": float(np.median([segment["bpm"] for segment in group])), "peaks": observed}
        candidates.update(_observed_beats(features, local))
    return sorted(candidates)


def _spectral_region(audio: wave.Wave_read, first: int, stop: int, *, hop: int,
                     fft_size: int, channels: int, width: int, frames: int,
                     rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Centered STFT channel powers retain antiphase stereo evidence."""
    sample_start = first * hop - fft_size // 2
    sample_stop = (stop - 1) * hop + fft_size // 2
    read_start, read_stop = max(0, sample_start), min(frames, sample_stop)
    audio.setpos(read_start)
    pieces: list[np.ndarray] = []
    remaining = read_stop - read_start
    # Each decoder input is at most two seconds, even with context present.
    while remaining:
        count = min(remaining, max(1, round(rate * CORE_SECONDS)))
        data = audio.readframes(count)
        decoded = _decode(data, width, channels)
        if len(decoded) != count:
            raise MusicStructureError("WAV PCM is truncated during percussion analysis")
        pieces.append(decoded.astype(np.float32))
        remaining -= count
    samples = np.concatenate(pieces) if pieces else np.empty((0, channels), dtype=np.float32)
    samples = np.pad(samples, ((max(0, -sample_start), max(0, sample_stop - frames)), (0, 0)))
    window = np.hanning(fft_size)
    power = np.zeros((stop - first, fft_size // 2 + 1))
    rms_energy = np.zeros(stop - first)
    # Process channels separately so the channel dimension does not multiply
    # the overlapping FFT working set. Signed stereo samples are never summed.
    for channel in range(channels):
        views = np.lib.stride_tricks.sliding_window_view(samples[:, channel], fft_size)[::hop]
        rms_energy += np.mean(views.astype(np.float64) ** 2, axis=1)
        spectrum = np.fft.rfft(views * window, axis=1)
        power += np.abs(spectrum) ** 2
    power /= channels
    power[:, 0] = 0  # DC belongs to file-integrity QC, not percussion evidence.
    return np.sqrt(power), np.sqrt(rms_energy / channels)


def _features(path: Path) -> tuple[np.ndarray, float, float, float]:
    try:
        with wave.open(str(path), "rb") as audio:
            channels, width, rate, total_frames = (audio.getnchannels(), audio.getsampwidth(),
                                                  audio.getframerate(), audio.getnframes())
            if audio.getcomptype() != "NONE" or width not in {1, 2, 3, 4}:
                raise MusicStructureError("only uncompressed 8/16/24/32-bit PCM WAV is supported")
            if not 1 <= channels <= 32 or not 1 <= rate <= 384_000 or total_frames <= 0:
                raise MusicStructureError("WAV has invalid or unsupported channel, rate, or frame metadata")
            duration = total_frames / rate
            if duration > MAX_AUDIO_SECONDS:
                raise MusicStructureError(f"percussion analysis supports at most {MAX_AUDIO_SECONDS:g} seconds")
            hop = max(1, round(rate * 0.01))
            fft_size = 1 << max(5, min(13, round(math.log2(max(32, rate * 0.046)))))
            observations = math.ceil(total_frames / hop)
            core = max(1, round(CORE_SECONDS * rate / hop))
            context = max(1, math.ceil(CONTEXT_SECONDS * rate / hop))
            time_kernel = 2 * max(1, round(0.25 * rate / hop)) + 1
            frequency_kernel = min(17, fft_size // 2 + 1)
            if frequency_kernel % 2 == 0:
                frequency_kernel -= 1
            weights = np.full(fft_size // 2 + 1, 2.0)
            weights[[0, -1]] = 1.0
            normalization = fft_size * float(np.sum(np.hanning(fft_size) ** 2))
            previous = np.zeros(fft_size // 2 + 1)
            batches: list[np.ndarray] = []
            for start in range(0, observations, core):
                end = min(observations, start + core)
                expanded_start, expanded_end = max(0, start - context), min(observations, end + context)
                magnitude, rms = _spectral_region(audio, expanded_start, expanded_end, hop=hop,
                    fft_size=fft_size, channels=channels, width=width, frames=total_frames, rate=rate)
                harmonic = median_filter(magnitude, size=(time_kernel, 1), mode="nearest")
                percussive = median_filter(magnitude, size=(1, frequency_kernel), mode="reflect")
                denominator = harmonic ** 2 + percussive ** 2
                mask = np.divide(percussive ** 2, denominator, out=np.zeros_like(magnitude),
                                 where=denominator > 1e-20)
                first, stop = start - expanded_start, end - expanded_start
                magnitude, mask, rms = magnitude[first:stop], mask[first:stop], rms[first:stop]
                transient = magnitude * mask
                sustained = magnitude * (1 - mask)
                percussion_rms = np.sqrt((transient ** 2 @ weights) / normalization)
                harmonic_rms = np.sqrt((sustained ** 2 @ weights) / normalization)
                preceding = np.vstack((previous, transient[:-1]))
                amplitude = transient.sum(axis=1)
                flux = np.divide(np.maximum(0, transient - preceding).sum(axis=1), amplitude,
                                 out=np.zeros(len(transient)), where=amplitude > 1e-12)
                # Mask leakage from a stationary tone must not become a pulse
                # merely because its tiny residual has a large relative change.
                flux[percussion_rms < np.maximum(0.0001, rms * 0.01)] = 0
                previous = transient[-1]
                times = np.arange(start, end) * hop / rate
                batches.append(np.column_stack((times, 20 * np.log10(np.maximum(rms, 1e-12)),
                                                flux, harmonic_rms, percussion_rms)))
            return np.concatenate(batches), hop / rate, fft_size / rate, duration
    except (OSError, wave.Error, EOFError) as error:
        raise MusicStructureError(f"WAV percussion analysis failed: {error}") from error


def _display_series(features: np.ndarray, hop: float, window: float) -> dict[str, Any]:
    stride = max(1, math.ceil(len(features) / MAX_DISPLAY_POINTS))
    points = []
    for start in range(0, len(features), stride):
        block = features[start:start + stride]
        point = np.mean(block, axis=0)
        energy = float(np.mean(10 ** (block[:, 1] / 10)))
        point[1] = 10 * math.log10(energy) if energy > 1e-22 else -240
        point[2] = np.max(block[:, 2])
        point[3:5] = np.sqrt(np.mean(block[:, 3:5] ** 2, axis=0))
        row: list[float | None] = [round(float(value), 7) for value in point]
        if energy <= 1e-22:
            row[1] = None
        points.append(row)
    return {"columns": FRAME_COLUMNS, "points": points, "maximumPoints": MAX_DISPLAY_POINTS,
            "analysisHopSeconds": hop, "analysisWindowSeconds": window, "intervalSeconds": hop * stride,
            "aggregation": "energy-averaged RMS; maximum flux; mean timestamp"}


def analyze_percussion(path: Path | str) -> dict[str, Any]:
    """Observe all transient onsets before a pulse-phase selection can hide jitter.

    HPSS components are spectral measurements, not reconstructed stems. Source
    reliability is a conservative repetition-evidence heuristic, not calibrated
    instrument-separation accuracy or proof that an onset belongs to a drum.
    """
    features, hop, window, duration = _features(Path(path))
    tempo = _tempo(features[:, 2], hop)
    peaks, _ = _onsets(features[:, 2], hop)
    segments = []
    segment_frames = max(1, round(12 / hop))
    for start in range(0, len(features), segment_frames):
        local = features[start:start + segment_frames]
        if len(local) * hop < MIN_RHYTHM_SECONDS:
            continue
        estimate = _tempo(local[:, 2], hop)
        segments.append({"startSeconds": round(float(local[0, 0]), 4),
                         "endSeconds": round(min(duration, float(local[-1, 0]) + hop), 4),
                         "bpm": estimate["bpm"], "confidence": estimate["confidence"],
                         "onsetCount": estimate["onsetCount"]})
    supported = [segment["confidence"] for segment in segments if segment["bpm"] is not None]
    evidence = max([tempo["confidence"] if tempo["bpm"] is not None else 0, *supported])
    reliability = round(min(0.7, 0.5 + 0.2 * evidence), 4) if evidence >= MIN_CONFIDENCE else 0.0
    # Reuse the independent phase selector only for explicitly named candidates.
    # Keep every raw onset separately so diagnosis can still examine outliers.
    beat_features = np.zeros((len(features), 8))
    beat_features[:, 0], beat_features[:, 7] = features[:, 0], features[:, 2]
    return {"version": VERSION, "method": "hpss-percussive-proxy",
            "status": "observed" if tempo["bpm"] is not None else "unknown",
            "pulse": {"bpm": tempo["bpm"], "confidence": tempo["confidence"],
                      "confidenceMeaning": "strength of transient repetition, not probability of the musical beat"},
            "segments": segments, "onsetTimesSeconds": [round(float(features[index, 0]), 4) for index in peaks],
            "beatCandidatesSeconds": _supported_beat_candidates(beat_features, tempo, segments),
            "frameSeries": _display_series(features, hop, window), "durationSeconds": duration,
            "source": {"kind": "percussive_estimate", "method": "median HPSS", "reliability": reliability,
                       "reliabilityMeaning": "conservative repetition evidence for a spectral proxy; not semantic separation accuracy"},
            "analysis": {"pcmBatchSeconds": CORE_SECONDS, "contextSeconds": CONTEXT_SECONDS,
                         "timeMedianSeconds": (2 * max(1, round(0.25 / hop)) + 1) * hop,
                         "frequencyMedianBins": 17, "mask": "soft squared median-magnitude ratio",
                         "minimumPercussiveRms": 0.0001, "minimumPercussiveToMixtureRmsRatio": 0.01},
            "limitations": ["Median HPSS estimates transient versus sustained spectral structure; it does not identify drums.",
                            "Harmonic HPSS is not backing and percussive HPSS is not a semantic drum stem.",
                            "This analysis cannot separate vocals from backing, verify downbeats or approve listening.",
                            "Transient vocal consonants, piano attacks and fills can remain in the percussive proxy.",
                            "Percussion below the conservative residual-activity floor may be unresolved."]}
