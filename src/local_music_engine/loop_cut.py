"""A seamless loop of a BGM version, cut on its own bars.

A generated song cannot loop as a file: it ends (the first BGM song heard ended at 85.4 s
and then held 4.6 s of near-silence) and starts from an intro. The loop is a stretch of
whole bars that starts and ends on a downbeat kick, chosen so the bar after its end sounds
like the bar it jumps back to. On that song a cut 30 ms before the kicks at 10.68 s and
74.68 s (24 bars, 64.000 s) was heard as natural at the seam.

Where things are measured:

- Tempo: the song's own downbeat kicks, not the played-tempo estimate. On that song a line
  through 24 downbeat kicks gave 90.0002 BPM with 1 ms residuals, while ``song_tempo`` said
  90.11, which makes 24 bars 78 ms short. ``song_tempo`` only picks the beat unit. The bar
  line may bend slowly (another song ran 90.42 BPM early and 90.53 late); the loop is cut at
  its two measured kicks, and their tempos must agree within 0.5%.
- Kicks: a zero-phase low-pass at 120 Hz, its Hilbert envelope, peaks at least a quarter of
  the loudest, and the onset where each peak's envelope first reaches half of it. This found
  the kicks heard on that song within 1 ms.
- Bars that match: per beat, five band levels, a 12-pitch chroma and the level; a loop's
  two ends must be no further apart than three quarters of the pairs of bars eight apart in
  the same song. The longest such loop wins; among ends that match about equally well, the
  earliest start (on that song bar 4, the heard cut, scored 0.117 and bar 5 0.109).
- The cut is taken from the mastered listening copy (what the app plays and export
  writes); the analysis reads the finished version, sample for sample the same timeline.

The join replaces the loop's first 20 ms with a fade from the audio that follows its end
into its start. Two near-identical windows are mixed linearly (an equal-power mix of the
heard song's seam peaked above its limiter ceiling); unrelated ones keep their power.
Only 4/4 is supported. Anything that cannot be measured is reported with a reason instead
of a loop.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import butter, find_peaks, hilbert, resample_poly, sosfiltfilt

from .music_structure import _decode
from .played_tempo import song_tempo

LOOP_VERSION = "loop-cut-v3"
KICK_CUTOFF_HZ = 120.0
ENVELOPE_RATE = 4000            # the low band is resampled here before its envelope
KICK_FLOOR = 0.1                # of the song's peak; the heard song's low band reached about half
MERGE_SECONDS = 0.05            # rises closer than this are one hit
PRE_ONSET_SECONDS = 0.030       # the cut sits this far before the downbeat kick
CROSSFADE_SECONDS = 0.020
LINEAR_FADE_CORRELATION = 0.5
GRID_TOLERANCE_SECONDS = 0.015  # a kick this close to a bar line is that bar's downbeat
BAR_SEARCH = 0.03               # bar length is searched within ±3% of four played beats
TEMPO_MATCH = 0.005             # the loop's two ends may differ in tempo by this much
RESIDUAL_RMS_SECONDS = 0.003     # drums measured 0.5–2.0 ms; a piano's low notes 4.2
RESIDUAL_MAX_SECONDS = 0.010
MINIMUM_DOWNBEATS = 8
LENGTH_STEP_BARS = 8
MINIMUM_BARS = 16
CONTROL_LAG_BARS = 8
MINIMUM_CONTROL_PAIRS = 4
MATCH_PERCENTILE = 75
MATCH_FLOOR = 0.5               # z units; identical repeats otherwise sit at a threshold of zero
MATCH_TIE = 0.05                # of the threshold: closer scores count as equal
PHASE_AMBIGUOUS_RATIO = 0.85
FEATURE_RATE = 16000
BANDS_HZ = ((30, 120), (120, 500), (500, 2000), (2000, 5000), (5000, 8000))
SEAM_MARGIN_DB = 2.0
GAIN_GUARD_DB = 0.5

REASONS = {
    "tempo_unknown": "템포를 잴 수 없어 반복용을 자르지 못했어요.",
    "tempo_unsteady": "템포가 일정하지 않아 반복용을 자르지 못했어요.",
    "no_kick": "마디 첫 박의 킥 드럼이 거의 없어 마디를 찾지 못했어요.",
    "groove_too_short": "드럼이 이어지는 부분이 짧아 16마디 이상 반복할 구간이 없어요.",
    "no_matching_bars": "처음과 끝이 맞는 마디를 찾지 못해 반복용을 자르지 못했어요.",
    "meter_unsupported": "4/4박자가 아닌 곡은 아직 반복용을 자르지 못해요.",
    "listening_copy_mismatch": "듣기용 사본과 버전의 길이가 달라 반복용을 자르지 못했어요.",
}


class LoopNotFound(ValueError):
    """No loop could be cut; ``code`` names why and ``reason`` says it to the listener."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.reason = REASONS[code]


def _header(path: Path) -> tuple[int, int, int, int]:
    with wave.open(str(path), "rb") as audio:
        if audio.getcomptype() != "NONE":
            raise ValueError(f"compressed WAV is unsupported: {path.name}")
        return audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()


def _read(path: Path, start: int = 0, count: int | None = None) -> np.ndarray:
    """Frames ``[start, start+count)`` as float32, one column per channel."""

    with wave.open(str(path), "rb") as audio:
        width, channels, total = audio.getsampwidth(), audio.getnchannels(), audio.getnframes()
        start = max(0, min(start, total))
        count = total - start if count is None else max(0, min(count, total - start))
        audio.setpos(start)
        return _decode(audio.readframes(count), width, channels).astype(np.float32)


def _mono(path: Path, chunk_seconds: float = 10.0) -> tuple[int, np.ndarray]:
    rate, _, _, total = _header(path)
    step = int(rate * chunk_seconds)
    parts = [_read(path, start, step).mean(axis=1) for start in range(0, total, step)]
    return rate, np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def kick_onsets(mono: np.ndarray, rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Onset times and envelope heights of the low-band hits."""

    low = sosfiltfilt(butter(4, KICK_CUTOFF_HZ, "low", fs=rate, output="sos"), mono.astype(np.float64))
    factor = max(1, rate // ENVELOPE_RATE)
    envelope_rate = rate / factor
    envelope = np.abs(hilbert(low[::factor]))
    envelope = uniform_filter1d(envelope, max(1, round(envelope_rate * 0.001)))
    # Without a kick the low band is only what leaks from higher parts; its peaks are not hits.
    if not len(envelope) or envelope.max() < KICK_FLOOR * float(np.abs(mono).max(initial=0.0)):
        return np.zeros(0), np.zeros(0)
    peaks, properties = find_peaks(envelope, height=0.25 * envelope.max(), distance=max(1, round(0.2 * envelope_rate)))
    onsets, heights = [], []
    for peak, height in zip(peaks, properties["peak_heights"]):
        index = int(peak)
        while index > 0 and envelope[index - 1] >= height / 2:
            index -= 1
        if index == 0:
            continue
        # Between the last sample below half and the first at or above it.
        below, above = envelope[index - 1], envelope[index]
        fraction = (height / 2 - below) / (above - below) if above > below else 0.0
        onset = (index - 1 + fraction) / envelope_rate
        # Several ripples of one hit's envelope lead back to the same rise; keep it once.
        if onsets and onset - onsets[-1] < MERGE_SECONDS:
            heights[-1] = max(heights[-1], float(height))
            continue
        onsets.append(onset)
        heights.append(float(height))
    return np.array(onsets), np.array(heights)


def _longest_steady_run(onsets: np.ndarray, prior_bar: float) -> int:
    """Bars in the longest chain of hits one bar apart whose spacing changes by under 10 ms per bar.

    A drifting kick still forms such a chain; bass notes of a piano do not.
    """

    longest = 0
    for first in range(len(onsets)):
        interval, at, run = None, onsets[first], 0
        while True:
            low, high = ((prior_bar * (1 - BAR_SEARCH), prior_bar * (1 + BAR_SEARCH)) if interval is None
                         else (interval - RESIDUAL_MAX_SECONDS, interval + RESIDUAL_MAX_SECONDS))
            following = onsets[(onsets >= at + low) & (onsets <= at + high)]
            if not len(following):
                break
            step = following[np.argmin(np.abs(following - at - (interval or prior_bar)))]
            interval, at, run = step - at, step, run + 1
        longest = max(longest, run)
    return longest


def _phase_scores(onsets: np.ndarray, weights: np.ndarray, bar: float, anchors: np.ndarray) -> np.ndarray:
    distance = np.mod(onsets[None, :] - anchors[:, None], bar)
    distance = np.minimum(distance, bar - distance)
    return (weights[None, :] * (distance <= GRID_TOLERANCE_SECONDS)).sum(axis=1)


def downbeat_grid(onsets: np.ndarray, heights: np.ndarray, *, prior_bar: float) -> dict[str, Any]:
    """The downbeats through the kicks: a bar line that may bend slowly, and how well it fits.

    A generated song can speed up and slow down a little: one heard as steady curved within
    ±11 ms of a straight bar line over 100 s, which a smooth curve follows within 6 ms.
    """

    if len(onsets) < MINIMUM_DOWNBEATS:
        raise LoopNotFound("no_kick")
    # Low-band hits that never repeat bar after bar are not a kick pattern, whatever their tempo.
    unsteady = "tempo_unsteady" if _longest_steady_run(onsets, prior_bar) >= MINIMUM_DOWNBEATS else "no_kick"
    weights = heights / heights.max()
    best = (-1.0, prior_bar, 0.0)
    for bar in np.arange(prior_bar * (1 - BAR_SEARCH), prior_bar * (1 + BAR_SEARCH), 0.0002):
        scores = _phase_scores(onsets, weights, bar, onsets)
        index = int(np.argmax(scores))
        if scores[index] > best[0]:
            best = (float(scores[index]), float(bar), float(onsets[index]))
    score, bar, anchor = best
    others = _phase_scores(onsets, weights, bar, anchor + bar * np.array([0.25, 0.5, 0.75]))
    ambiguous = bool(others.max() >= PHASE_AMBIGUOUS_RATIO * score)
    # Start from the straight line, then let the curve gather the downbeats it predicts.
    coefficients = np.array([0.0, bar, anchor])
    numbers = np.arange(math.floor((onsets[0] - anchor) / bar) - 1, math.ceil((onsets[-1] - anchor) / bar) + 2)
    times = np.array([])
    for _ in range(4):
        predicted = np.polyval(coefficients, numbers)
        nearest = np.abs(onsets[None, :] - predicted[:, None]).argmin(axis=1)
        near = np.abs(onsets[nearest] - predicted) <= GRID_TOLERANCE_SECONDS
        times, found = onsets[nearest[near]], numbers[near]
        if len(times) < MINIMUM_DOWNBEATS:
            raise LoopNotFound(unsteady)
        coefficients = np.polyfit(found, times, 2)
    initial = len(times)
    residuals = times - np.polyval(coefficients, found)
    keep = np.abs(residuals) <= RESIDUAL_MAX_SECONDS
    times, found = times[keep], found[keep]
    if len(times) < max(MINIMUM_DOWNBEATS, 0.75 * initial):
        raise LoopNotFound(unsteady)
    coefficients = np.polyfit(found, times, 2)
    residuals = times - np.polyval(coefficients, found)
    rms = float(np.sqrt(np.mean(residuals ** 2)))
    if rms > RESIDUAL_RMS_SECONDS or np.abs(residuals).max() > RESIDUAL_MAX_SECONDS:
        raise LoopNotFound(unsteady)
    # Number bars from the first whole bar of the song.
    first = int(numbers[0])
    while np.polyval(coefficients, first - 1) >= 0:
        first -= 1
    while np.polyval(coefficients, first) < 0:
        first += 1
    kicks = {int(number) - first: float(time) for number, time in zip(found, times)}
    return {"coefficients": [float(value) for value in coefficients], "firstBar": first,
            "barSeconds": float(np.mean([bar_length({"coefficients": coefficients, "firstBar": first}, k)
                                         for k in kicks])),
            "originSeconds": float(np.polyval(coefficients, first)), "downbeatKicks": kicks,
            "residualRmsSeconds": rms, "residualMaxSeconds": float(np.abs(residuals).max()),
            "phaseAmbiguous": ambiguous}


def bar_time(grid: dict[str, Any], bar: float) -> float:
    return float(np.polyval(grid["coefficients"], bar + grid["firstBar"]))


def bar_length(grid: dict[str, Any], bar: float) -> float:
    a, b, _ = grid["coefficients"]
    return float(2 * a * (bar + grid["firstBar"] + 0.5) + b)


def _chroma_bins(size: int, rate: int) -> tuple[np.ndarray, np.ndarray]:
    frequencies = np.fft.rfftfreq(size, 1 / rate)
    usable = np.flatnonzero((frequencies >= 65) & (frequencies <= 2000))
    pitch = np.mod(np.round(12 * np.log2(frequencies[usable] / 440.0)).astype(int) + 9, 12)
    return usable, pitch


def bar_features(mono: np.ndarray, rate: int, grid: dict[str, Any], bars: int) -> np.ndarray:
    """One row per bar: per beat, five band levels (dB), a 12-pitch chroma and the level (dB)."""

    audio = resample_poly(mono.astype(np.float64), FEATURE_RATE, rate) if rate != FEATURE_RATE else mono.astype(np.float64)
    size = int(round(grid["barSeconds"] / 4 * FEATURE_RATE))
    window = np.hanning(size)
    frequencies = np.fft.rfftfreq(size, 1 / FEATURE_RATE)
    bands = [np.flatnonzero((frequencies >= low) & (frequencies < high)) for low, high in BANDS_HZ]
    usable, pitch = _chroma_bins(size, FEATURE_RATE)
    rows = []
    for bar in range(bars):
        row = []
        for step in range(4):
            start = int(round((bar_time(grid, bar) + step * bar_length(grid, bar) / 4) * FEATURE_RATE))
            piece = audio[start:start + size]
            if len(piece) < size:
                piece = np.pad(piece, (0, size - len(piece)))
            magnitude = np.abs(np.fft.rfft(piece * window))
            power = magnitude ** 2
            row.extend(10 * np.log10(power[band].sum() + 1e-12) for band in bands)
            chroma = np.bincount(pitch, weights=magnitude[usable], minlength=12)
            row.extend(chroma / (chroma.sum() + 1e-12))
            row.append(10 * np.log10(float(np.mean(piece ** 2)) + 1e-12))
        rows.append(row)
    return np.array(rows)


def _distances(features: np.ndarray) -> np.ndarray:
    """Distance between every pair of bars, each feature in units of its spread over the song."""

    floors = np.tile(np.r_[np.full(len(BANDS_HZ), 1.0), np.full(12, 0.02), 1.0], 4)
    scaled = (features - features.mean(axis=0)) / np.maximum(features.std(axis=0), floors)
    difference = scaled[:, None, :] - scaled[None, :, :]
    return np.sqrt(np.mean(difference ** 2, axis=2))


def choose_loop(grid: dict[str, Any], features: np.ndarray) -> dict[str, Any]:
    """The longest whole-bar loop whose ends match, then the closest match, then the earliest."""

    bars = len(features)
    kicks = grid["downbeatKicks"]
    distance = _distances(features)

    def pair(first: int, second: int) -> float:
        # The bar after the end is replaced by the loop's first bar, and so on for one more.
        return float((distance[first, second] + distance[first + 1, second + 1]) / 2)

    controls = [pair(bar, bar + CONTROL_LAG_BARS) for bar in range(bars - CONTROL_LAG_BARS - 1)
                if bar in kicks and bar + CONTROL_LAG_BARS in kicks]
    if len(controls) < MINIMUM_CONTROL_PAIRS:
        raise LoopNotFound("groove_too_short")
    threshold = max(float(np.percentile(controls, MATCH_PERCENTILE)), MATCH_FLOOR)
    any_kicks, any_tempo = False, False
    for length in range((bars - 2) // LENGTH_STEP_BARS * LENGTH_STEP_BARS, MINIMUM_BARS - 1, -LENGTH_STEP_BARS):
        candidates = []
        for start in range(0, bars - length - 1):
            end = start + length
            if not (start in kicks and end in kicks and end + 1 in kicks):
                continue
            any_kicks = True
            # The wrap jumps from the end's tempo back to the start's; they must be the same to the ear.
            if abs(bar_length(grid, end) / bar_length(grid, start) - 1) > TEMPO_MATCH:
                continue
            any_tempo = True
            candidates.append((pair(start, end), start))
        passing = [candidate for candidate in candidates if candidate[0] <= threshold]
        if passing:
            # Matches this close are the same to the ear; the earlier start keeps more of the song.
            best = min(score for score, _ in passing)
            score, start = min((candidate for candidate in passing if candidate[0] <= best + MATCH_TIE * threshold),
                               key=lambda candidate: candidate[1])
            return {"startBar": start, "endBar": start + length, "bars": length, "score": round(score, 3),
                    "threshold": round(threshold, 3), "controlPairs": len(controls), "candidates": len(candidates)}
    raise LoopNotFound("no_matching_bars" if any_tempo else "tempo_unsteady" if any_kicks else "groove_too_short")


def _rms_db(audio: np.ndarray) -> float:
    return 10 * math.log10(float(np.mean(np.square(audio, dtype=np.float64))) + 1e-12)


def find_loop(analysis: Path | str, listening: Path | str, *, reference_bpm: float | None,
              time_signature: str | None) -> dict[str, Any]:
    """Where to cut, measured on ``analysis`` (the finished version); raises :class:`LoopNotFound`."""

    analysis, listening = Path(analysis), Path(listening)
    warnings = []
    if time_signature not in (None, "", "4"):
        raise LoopNotFound("meter_unsupported")
    if not time_signature:
        warnings.append("meter_assumed")
    rate, channels, _, frames = _header(analysis)
    listening_rate, _, _, listening_frames = _header(listening)
    if (listening_rate, listening_frames) != (rate, frames):
        raise LoopNotFound("listening_copy_mismatch")
    tempo = song_tempo(analysis, reference_bpm=reference_bpm, reference_source="request")
    if tempo.get("status") != "measured":
        raise LoopNotFound("tempo_unknown")
    _, mono = _mono(analysis)
    onsets, heights = kick_onsets(mono, rate)
    grid = downbeat_grid(onsets, heights, prior_bar=4 * 60 / tempo["playedBpm"])
    if grid["phaseAmbiguous"]:
        warnings.append("phase_ambiguous")
    bars = 0
    while bar_time(grid, bars + 1) <= len(mono) / rate:
        bars += 1
    features = bar_features(mono, rate, grid, bars)
    choice = choose_loop(grid, features)
    # Both ends sit the same distance before their own measured kick, so the groove carries on.
    start_kick = grid["downbeatKicks"][choice["startBar"]]
    end_kick = grid["downbeatKicks"][choice["endBar"]]
    start_frame = int(round((start_kick - PRE_ONSET_SECONDS) * rate))
    end_frame = int(round((end_kick - PRE_ONSET_SECONDS) * rate))
    loop_bar = (end_kick - start_kick) / choice["bars"]
    fade = int(round(CROSSFADE_SECONDS * rate))
    if start_frame < 0 or end_frame + fade > frames:
        raise LoopNotFound("groove_too_short")
    return {"version": LOOP_VERSION, "sampleRate": rate, "channels": channels,
            "startFrame": start_frame, "endFrame": end_frame,
            "startSeconds": round(start_frame / rate, 6), "endSeconds": round(end_frame / rate, 6),
            "durationSeconds": round((end_frame - start_frame) / rate, 6),
            "bars": choice["bars"], "beatsPerBar": 4, "barSeconds": round(loop_bar, 6),
            "bpm": round(4 * 60 / loop_bar, 3), "startBar": choice["startBar"],
            "tempo": {"playedTempoBpm": tempo["playedBpm"], "downbeatBpm": round(4 * 60 / grid["barSeconds"], 4),
                      "loopEndsBpm": [round(4 * 60 / bar_length(grid, choice[key]), 3) for key in ("startBar", "endBar")],
                      "residualRmsMs": round(grid["residualRmsSeconds"] * 1000, 2),
                      "residualMaxMs": round(grid["residualMaxSeconds"] * 1000, 2),
                      "downbeatKicks": len(grid["downbeatKicks"])},
            "match": {key: choice[key] for key in ("score", "threshold", "controlPairs", "candidates")},
            "warnings": warnings}


def _write_pcm24(path: Path, audio: np.ndarray, rate: int) -> None:
    integers = np.clip(np.round(audio.astype(np.float64) * (1 << 23)), -(1 << 23), (1 << 23) - 1).astype("<i4")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(audio.shape[1])
        output.setsampwidth(3)
        output.setframerate(rate)
        output.writeframes(integers.view(np.uint8).reshape(-1, 4)[:, :3].tobytes())


def render_loop(listening: Path | str, destination: Path | str, plan: dict[str, Any], *,
                analysis: Path | str | None = None) -> dict[str, Any]:
    """Write the loop to ``destination`` (a new file) and return how the seam was made and measured."""

    listening, destination = Path(listening), Path(destination)
    rate, start, end = plan["sampleRate"], plan["startFrame"], plan["endFrame"]
    fade = int(round(CROSSFADE_SECONDS * rate))
    loop = _read(listening, start, end - start).copy()
    tail = _read(listening, end, fade).astype(np.float64)
    head = loop[:fade].astype(np.float64)
    source_peak = max(float(np.abs(_read(listening, offset, rate * 10)).max(initial=0.0))
                      for offset in range(0, _header(listening)[3], rate * 10))
    correlation = float(np.corrcoef(tail.ravel(), head.ravel())[0, 1]) if tail.std() > 0 and head.std() > 0 else 0.0
    ramp = (np.arange(fade) + 0.5) / fade
    law = "linear"
    mixed = tail * (1 - ramp)[:, None] + head * ramp[:, None]
    if correlation < LINEAR_FADE_CORRELATION:
        powered = tail * np.cos(ramp * np.pi / 2)[:, None] + head * np.sin(ramp * np.pi / 2)[:, None]
        # A linear mix of two signals never exceeds the louder; an equal-power one can.
        if np.abs(powered).max() <= source_peak:
            mixed, law = powered, "equal-power"
    loop[:fade] = mixed.astype(np.float32)
    _write_pcm24(destination, loop, rate)
    beat = int(round(plan["barSeconds"] / 4 * rate))
    # The level change across the seam, against the change across each bar line inside the loop.
    seam = abs(_rms_db(loop[-beat:]) - _rms_db(loop[:beat]))
    inside = [abs(_rms_db(loop[point - beat:point]) - _rms_db(loop[point:point + beat]))
              for point in (int(round(bar * plan["barSeconds"] * rate)) for bar in range(1, plan["bars"]))]
    control = float(np.percentile(inside, 90)) if inside else 0.0
    warnings = list(plan["warnings"])
    if seam > control + SEAM_MARGIN_DB:
        warnings.append("seam_level_differs")
    result = {"crossfade": {"seconds": CROSSFADE_SECONDS, "law": law, "correlation": round(correlation, 3)},
              "seam": {"levelChangeDb": round(seam, 2), "barLineChangeP90Db": round(control, 2)},
              "peak": round(float(np.abs(loop).max()), 6), "sourcePeak": round(source_peak, 6)}
    if analysis is not None:
        # The cut was chosen on the finished version and taken from its mastered copy; the two
        # ends should have met the limiter alike.
        gains = []
        for point in (start, end):
            offset = int(0.02 * rate)
            mastered = _read(listening, point + offset, int(0.28 * rate))
            finished = _read(Path(analysis), point + offset, int(0.28 * rate))
            gains.append(_rms_db(mastered) - _rms_db(finished))
        result["masteringGainDb"] = [round(gain, 2) for gain in gains]
        if abs(gains[0] - gains[1]) > GAIN_GUARD_DB:
            warnings.append("mastering_gain_differs")
    result["warnings"] = warnings
    return result
