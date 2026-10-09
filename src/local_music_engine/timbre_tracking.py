"""Measured hits linked to their own repetitions, independently of beat models.

Every attack measured in the PCM is matched, by its multi-band attack shape, to
the corresponding attack in earlier and later repetitions of the same music.
Agreement between several repetitions gives that hit's measured displacement.
A smooth curve through those displacements is an expected timing: it is never
a measurement, and no model beat or requested BPM is consulted at any step.

Music without repeated rhythm supplies no reference, so those ranges abstain.
Only compact band envelopes span the track; PCM and FFT frames stay bounded.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import find_peaks

from .music_structure import MAX_AUDIO_SECONDS, MusicStructureError, _decode
from .storage import sha256_file

VERSION = "timbre-timing-v2"
CORE_SECONDS = 2.0
EXPECTED_TIMING_KIND = "smooth_curve_through_repetition_referenced_hits_not_model_or_measurement"
PARAMETERS = {
    "hopSeconds": 0.01, "windowSeconds": 0.046, "spectralBands": 8,
    # A hit's attack shape: 30 ms before to 50 ms after its measured peak.
    "attackFramesBefore": 3, "attackFramesAfter": 5,
    "searchSeconds": 0.30, "minimumMatchQuality": 0.5,
    # A link may drift between neighbouring hits, but a jump faster than this
    # is a different rhythm, not the same hit played early or late.
    "maximumDisplacementSlope": 0.6, "displacementSlackSeconds": 0.02, "displacementChangePenalty": 1.5,
    "minimumLagSeconds": 1.0, "maximumLagSeconds": 24.0, "minimumRepetitionsOfLag": 2.5,
    "lagsPerOctaveBand": 3, "minimumRepetitionStrength": 0.15, "minimumRelativeRepetitionStrength": 0.4,
    "driftBaselineSeconds": 10.0, "minimumLinkedHitsPerReference": 8,
    "minimumAgreeingReferences": 3, "agreementSeconds": 0.015, "minimumAgreeingShare": 1 / 3,
    "maximumRivalShare": 0.75, "minimumReferenceSpreadSeconds": 8.0, "minimumInPlaceReferences": 3,
    "expectedCurveSeconds": 2.0, "minimumExpectedCurveHits": 8,
    "minimumDeviationSeconds": 0.015, "deviationNoiseMultiple": 4.0,
    # Farther than this from its expected time, a counterpart is more likely a
    # different hit of the pattern than the same hit played early or late.
    "maximumDeviationSeconds": 0.12,
    "supportWindowSeconds": 1.5, "minimumHitsPerSupportWindow": 5,
    "maximumEvidenceGapSeconds": 3.0, "minimumEvidenceScore": 4.0, "minimumEvidencePerSecond": 1.0,
    "minimumDeviantHits": 5, "minimumDisturbanceSeconds": 1.0, "eventMarginSeconds": 0.1,
    "minimumSmoothChangeSeconds": 0.05, "minimumSmoothChangeDurationSeconds": 2.0,
}
LAG_BANDS_SECONDS = ((1.0, 3.0), (3.0, 6.0), (6.0, 12.0), (12.0, 24.001))
HIT_COLUMNS = ["measuredTimeSeconds", "expectedTimeSeconds", "residualSeconds",
               "displacementSeconds", "agreeingReferences", "linkedReferences"]


def _band_energy(audio: wave.Wave_read, first: int, stop: int, *, hop: int, window: int, fft_size: int,
                 bands: np.ndarray, channels: int, width: int, frames: int) -> np.ndarray:
    """Band magnitudes of centred frames; channel powers add, so antiphase stereo keeps its evidence."""
    sample_start = first * hop - window // 2
    sample_stop = (stop - 1) * hop - window // 2 + window
    read_start, read_stop = max(0, sample_start), min(frames, sample_stop)
    audio.setpos(read_start)
    samples = _decode(audio.readframes(read_stop - read_start), width, channels)
    if len(samples) != read_stop - read_start:
        raise MusicStructureError("WAV PCM is truncated during timbre tracking")
    samples = np.pad(samples, ((read_start - sample_start, sample_stop - read_stop), (0, 0)))
    taper = np.hanning(window)
    power = np.zeros((stop - first, fft_size // 2 + 1))
    for channel in range(channels):
        views = np.lib.stride_tricks.sliding_window_view(samples[:, channel], window)[::hop]
        power += np.abs(np.fft.rfft(views * taper, n=fft_size, axis=1)) ** 2
    power[:, 0] = 0  # DC belongs to file-integrity QC, not to an attack.
    return np.sqrt(power @ bands)


def _novelty(path: Path) -> tuple[np.ndarray, float, float, float]:
    """Positive log-level rises in a few broad bands, gain-invariant by construction.

    The window has the same duration at every sample rate, so timing evidence
    does not depend on how the file happens to be sampled.
    """
    with wave.open(str(path), "rb") as audio:
        rate, channels, width, frames = (audio.getframerate(), audio.getnchannels(),
                                         audio.getsampwidth(), audio.getnframes())
        if (audio.getcomptype() != "NONE" or width not in (1, 2, 3, 4)
                or not 1 <= channels <= 32 or not 1 <= rate <= 384_000 or frames <= 0):
            raise MusicStructureError("Invalid or unsupported PCM WAV metadata")
        duration = frames / rate
        if duration > MAX_AUDIO_SECONDS:
            raise MusicStructureError("Timbre tracking audio exceeds duration limit")
        hop = max(1, round(rate * PARAMETERS["hopSeconds"]))
        window = max(16, round(rate * PARAMETERS["windowSeconds"]))
        fft_size = 1 << (window - 1).bit_length()
        frequencies = np.fft.rfftfreq(fft_size, 1 / rate)
        edges = np.geomspace(40, max(41, min(16000, rate / 2)), PARAMETERS["spectralBands"] + 1)
        bands = np.array([(frequencies >= low) & (frequencies < high)
                          for low, high in zip(edges[:-1], edges[1:])], float).T
        bands = bands[:, bands.sum(axis=0) > 0]
        total = math.ceil(frames / hop)
        core = max(1, round(CORE_SECONDS * rate / hop))
        # Two-second batches bound the decoded PCM and FFT frames held at once.
        energy = np.concatenate([_band_energy(audio, first, min(total, first + core), hop=hop, window=window,
                                              fft_size=fft_size, bands=bands, channels=channels, width=width,
                                              frames=frames) for first in range(0, total, core)])
    active = energy[energy > 0]
    if not len(active) or energy.shape[1] < 2:
        return np.zeros((len(energy), max(1, energy.shape[1]))), duration, hop / rate, window / rate
    level = np.log1p(10 * energy / float(np.median(active)))
    rise = np.maximum(0, level[2:] - level[:-2])
    return np.vstack((np.zeros((2, energy.shape[1])), rise)), duration, hop / rate, window / rate


def _hits(novelty: np.ndarray, hop: float, margin: int) -> np.ndarray:
    """Attack peaks that stand out against the track's typical attack level.

    A loud passage is judged by its own level. A sparse one is not: faint
    ripples in a breakdown are texture, and treating them as hits would link
    them to arbitrary attacks elsewhere.
    """
    envelope = novelty.sum(axis=1)
    if not np.any(envelope > 0):
        return np.empty(0, dtype=np.int64)
    scale = np.sqrt(np.maximum(uniform_filter1d(envelope ** 2, size=max(1, round(4 / hop)), mode="nearest"), 0))
    relative = envelope / np.maximum(scale, float(np.median(scale[scale > 0])))
    peaks, _ = find_peaks(relative, distance=max(1, round(0.05 / hop)), height=0.6, prominence=0.3)
    return peaks[(peaks >= margin) & (peaks < len(novelty) - margin)]


def _repetition_lags(novelty: np.ndarray, duration: float, hop: float) -> tuple[list[int], list[float]]:
    """Lags at which the multi-band attack pattern recurs; no tempo or meter is assumed."""
    count = len(novelty)
    first = round(PARAMETERS["minimumLagSeconds"] / hop)
    last = min(round(PARAMETERS["maximumLagSeconds"] / hop),
               int(duration / PARAMETERS["minimumRepetitionsOfLag"] / hop))
    if last <= first + 2:
        return [], []
    size = 1 << (2 * count - 1).bit_length()
    correlation = np.zeros(count)
    for band in range(novelty.shape[1]):
        spectrum = np.fft.rfft(novelty[:, band] - novelty[:, band].mean(), n=size)
        correlation += np.fft.irfft(spectrum * spectrum.conj(), n=size)[:count]
    if correlation[0] <= 0:
        return [], []
    correlation = correlation / np.maximum(count - np.arange(count), 1) / (correlation[0] / count)
    peaks, _ = find_peaks(correlation[first:last], distance=max(1, round(0.25 / hop)))
    peaks = peaks + first
    if not len(peaks):
        return [], []
    floor = max(PARAMETERS["minimumRepetitionStrength"],
                PARAMETERS["minimumRelativeRepetitionStrength"] * float(np.max(correlation[peaks])))
    peaks = peaks[correlation[peaks] >= floor]
    chosen: list[int] = []
    # Short lags repeat best, but only longer ones look outside a disturbed passage.
    for low, high in LAG_BANDS_SECONDS:
        band = peaks[(peaks * hop >= low) & (peaks * hop < high)]
        chosen.extend(sorted(band[np.argsort(-correlation[band], kind="stable")][:PARAMETERS["lagsPerOctaveBand"]].tolist()))
    return chosen, [float(correlation[lag]) for lag in chosen]


def _candidates(novelty: np.ndarray, hits: np.ndarray, lag: int, hop: float) -> list[list[tuple[float, float]]]:
    """Where each hit's attack shape recurs near the reference: (offset seconds, quality)."""
    before, after = PARAMETERS["attackFramesBefore"], PARAMETERS["attackFramesAfter"]
    search = round(PARAMETERS["searchSeconds"] / hop)
    result: list[list[tuple[float, float]]] = [[] for _ in hits]
    usable = np.flatnonzero((hits + lag - before - search >= 0) & (hits + lag + after + search < len(novelty)))
    if not len(usable):
        return result
    span = np.arange(-before, after + 1)
    shapes = novelty[hits[usable][:, None] + span].reshape(len(usable), -1)
    shape_norms = np.linalg.norm(shapes, axis=1)
    offsets = np.arange(-search, search + 1)
    quality = np.empty((len(usable), len(offsets)))
    for column, offset in enumerate(offsets):
        reference = novelty[hits[usable][:, None] + lag + offset + span].reshape(len(usable), -1)
        quality[:, column] = (np.einsum("ij,ij->i", shapes, reference)
                              / np.maximum(shape_norms * np.linalg.norm(reference, axis=1), 1e-12))
    left, middle, right = quality[:, :-2], quality[:, 1:-1], quality[:, 2:]
    rows, columns = np.nonzero((middle > left) & (middle >= right) & (middle >= PARAMETERS["minimumMatchQuality"]))
    curvature = left[rows, columns] - 2 * middle[rows, columns] + right[rows, columns]
    # Parabolic refinement resolves the peak below the 10 ms frame spacing.
    shift = np.where(curvature < 0, 0.5 * (left[rows, columns] - right[rows, columns]) / np.where(curvature < 0, curvature, 1), 0)
    for row, column, fraction in zip(rows, columns, shift):
        result[usable[row]].append((float((offsets[column + 1] + fraction) * hop), float(middle[row, column])))
    return result


def _link(times: np.ndarray, candidates: list[list[tuple[float, float]]]) -> np.ndarray:
    """Choose one counterpart per hit, or none, so that the offset evolves plausibly.

    States from the last two seconds constrain the next link exactly; anything
    older contributes only its best score. A hit without a plausible
    counterpart stays unlinked instead of borrowing a neighbouring attack.
    """
    slope, slack = PARAMETERS["maximumDisplacementSlope"], PARAMETERS["displacementSlackSeconds"]
    penalty, floor = PARAMETERS["displacementChangePenalty"], PARAMETERS["minimumMatchQuality"]
    scores: list[float] = []
    state_times: list[float] = []
    state_offsets: list[float] = []
    state_hits: list[int] = []
    previous: list[int] = []
    older_best, older_state, oldest_recent = 0.0, -1, 0
    for index, time in enumerate(times):
        if not candidates[index]:
            continue
        while oldest_recent < len(scores) and time - state_times[oldest_recent] > 2.0:
            if scores[oldest_recent] > older_best:
                older_best, older_state = scores[oldest_recent], oldest_recent
            oldest_recent += 1
        recent = slice(oldest_recent, len(scores))
        recent_scores = np.asarray(scores[recent])
        recent_offsets = np.asarray(state_offsets[recent])
        gaps = time - np.asarray(state_times[recent])
        additions = []
        for offset, quality in candidates[index]:
            best, origin = older_best, older_state
            if len(recent_scores):
                change = np.abs(offset - recent_offsets)
                value = np.where(change <= slope * gaps + slack, recent_scores - penalty * change, -np.inf)
                position = int(np.argmax(value))
                if value[position] > best:
                    best, origin = float(value[position]), oldest_recent + position
            additions.append((best + quality - floor, offset, origin))
        for score, offset, origin in additions:
            scores.append(score)
            state_times.append(float(time))
            state_offsets.append(offset)
            state_hits.append(index)
            previous.append(origin)
    linked = np.full(len(times), np.nan)
    if not scores:
        return linked
    state = int(np.argmax(scores))
    while state >= 0:
        linked[state_hits[state]] = state_offsets[state]
        state = previous[state]
    return linked


def _running_median(times: np.ndarray, values: np.ndarray, half: float, minimum: int) -> np.ndarray:
    result = np.full(len(times), np.nan)
    left = np.searchsorted(times, times - half, "left")
    right = np.searchsorted(times, times + half, "right")
    for index in range(len(times)):
        if right[index] - left[index] >= minimum:
            result[index] = np.median(values[left[index]:right[index]])
    return result


def _consensus(table: np.ndarray, lags: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A hit's displacement is what several separate repetitions agree on.

    References inside the same disturbed passage disagree with each other, so
    they lose to the references outside it. Agreement must come from
    repetitions spread across time: one other passage cannot outvote the rest.
    """
    count = table.shape[1]
    displacement, agreeing = np.full(count, np.nan), np.zeros(count, dtype=np.int64)
    linked = np.count_nonzero(~np.isnan(table), axis=0)
    for index in np.flatnonzero(linked >= PARAMETERS["minimumAgreeingReferences"]):
        present = ~np.isnan(table[:, index])
        values, sources = table[present, index], lags[present]
        on_time = np.abs(values) <= PARAMETERS["agreementSeconds"]
        distant = on_time & (np.abs(sources) >= PARAMETERS["minimumReferenceSpreadSeconds"])
        # Distant repetitions that reproduce the hit in place make it a recurring
        # variation: it is on time whatever the closer repetitions suggest.
        if distant.sum() >= PARAMETERS["minimumInPlaceReferences"]:
            displacement[index], agreeing[index] = float(np.mean(values[distant])), int(on_time.sum())
            continue
        near = np.abs(values[:, None] - values[None, :]) <= PARAMETERS["agreementSeconds"]
        votes = near.sum(axis=1)
        best = int(np.lexsort((np.abs(values), -votes))[0])
        group = near[best]
        # An earlier and a later repetition must both agree: links to one other
        # passage only compare two different passages, not a hit with itself.
        if (votes[best] < PARAMETERS["minimumAgreeingReferences"]
                or votes[best] < PARAMETERS["minimumAgreeingShare"] * len(values)
                or np.ptp(sources[group]) < PARAMETERS["minimumReferenceSpreadSeconds"]
                or not (np.any(sources[group] > 0) and np.any(sources[group] < 0))):
            continue
        others = ~group
        if others.any() and near[others][:, others].sum(axis=1).max() >= PARAMETERS["maximumRivalShare"] * votes[best]:
            continue
        displacement[index], agreeing[index] = float(np.mean(values[group])), int(votes[best])
    return displacement, agreeing, linked


def _expected_curve(times: np.ndarray, values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Local least-absolute-deviation quadratic through the measured displacements.

    The convex criterion follows a gradual tempo change, ignores a minority of
    stray hits, and cannot lock onto one side of a rapid oscillation.
    """
    half, minimum = PARAMETERS["expectedCurveSeconds"], PARAMETERS["minimumExpectedCurveHits"]
    result = np.full(len(times), np.nan)
    left = np.searchsorted(times, times - half, "left")
    right = np.searchsorted(times, times + half, "right")
    for index in range(len(times)):
        first, stop = left[index], right[index]
        if stop - first < minimum:
            continue
        offset, local, weight = times[first:stop] - times[index], values[first:stop], weights[first:stop]
        design = np.vstack((np.ones(len(offset)), offset, offset * offset)).T
        coefficients = np.array([float(np.median(local)), 0.0, 0.0])
        for _ in range(30):
            emphasis = weight / np.maximum(np.abs(local - design @ coefficients), 0.002)
            weighted = design * emphasis[:, None]
            try:
                updated = np.linalg.solve(design.T @ weighted, weighted.T @ local)
            except np.linalg.LinAlgError:
                break
            settled = float(np.max(np.abs(updated - coefficients))) < 2e-5
            coefficients = updated
            if settled:
                break
        result[index] = coefficients[0]
    return result


def _evidence_runs(times: np.ndarray, scores: np.ndarray) -> list[tuple[int, int, float]]:
    """Maximal runs of hits whose deviating evidence outweighs their on-time evidence."""
    cuts = [0, *(np.flatnonzero(np.diff(times) > PARAMETERS["maximumEvidenceGapSeconds"]) + 1).tolist(), len(times)]
    pending = list(zip(cuts[:-1], cuts[1:]))
    found = []
    while pending:
        first, stop = pending.pop()
        best, best_range, running, start = 0.0, None, 0.0, first
        for index in range(first, stop):
            if running <= 0:
                running, start = 0.0, index
            running += scores[index]
            if running > best:
                best, best_range = running, (start, index)
        if best_range is None:
            continue
        found.append((best_range[0], best_range[1], float(best)))
        pending.extend(((first, best_range[0]), (best_range[1] + 1, stop)))
    return sorted(found)


def _ranges(mask: np.ndarray, centres: np.ndarray, half: float, duration: float) -> list[list[float]]:
    merged: list[list[float]] = []
    edges = np.flatnonzero(np.diff(np.r_[False, mask, False].astype(np.int8)))
    for first, stop in zip(edges[::2], edges[1::2]):
        start, end = max(0.0, float(centres[first]) - half), min(duration, float(centres[stop - 1]) + half)
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [[round(start, 4), round(end, 4)] for start, end in merged]


def _event(check: str, severity: str, start: float, end: float, confidence: float, status: str,
           observed: dict[str, Any], threshold: dict[str, Any], message: str) -> dict[str, Any]:
    return {"check": check, "category": "beat_timing", "severity": severity,
            "confidence": round(min(1.0, max(0.0, confidence)), 4),
            "startSeconds": round(start, 4), "endSeconds": round(end, 4), "evidenceStatus": status,
            "observed": observed, "threshold": threshold, "message": message, "retryEligible": False}


def analyze_timbre_timing(audio: Path | str) -> dict[str, Any]:
    """Measure hit timing against the track's own repetitions; read-only, no model."""
    path = Path(audio).expanduser().resolve()
    source_bytes = path.stat().st_size
    source_hash = sha256_file(path)
    try:
        novelty, duration, hop, window = _novelty(path)
    except (OSError, wave.Error, EOFError) as error:
        raise MusicStructureError(f"Timbre timing WAV read failed: {error}") from error
    search = round(PARAMETERS["searchSeconds"] / hop)
    hits = _hits(novelty, hop, PARAMETERS["attackFramesBefore"] + PARAMETERS["attackFramesAfter"] + search + 1)
    times = hits * hop
    lags, strengths = _repetition_lags(novelty, duration, hop) if len(hits) else ([], [])
    reference_lags, tables = [], []
    for lag in lags:
        for direction in (1, -1):
            # A counterpart found later than its nominal position means this hit came early.
            relative = -_link(times, _candidates(novelty, hits, direction * lag, hop))
            linked = ~np.isnan(relative)
            if linked.sum() < PARAMETERS["minimumLinkedHitsPerReference"]:
                continue
            # Slow drift against one reference is tempo, not a displaced hit.
            relative[linked] -= _running_median(times[linked], relative[linked], PARAMETERS["driftBaselineSeconds"],
                                                PARAMETERS["minimumLinkedHitsPerReference"])
            reference_lags.append(direction * lag * hop)
            tables.append(relative)
    count = len(hits)
    displacement, agreeing, linked = (_consensus(np.asarray(tables), np.asarray(reference_lags)) if tables
                                      else (np.full(count, np.nan), np.zeros(count, dtype=np.int64), np.zeros(count, dtype=np.int64)))
    measured = np.flatnonzero(~np.isnan(displacement))
    expected = np.full(count, np.nan)
    if len(measured) >= PARAMETERS["minimumExpectedCurveHits"]:
        expected[measured] = _expected_curve(times[measured], displacement[measured], agreeing[measured].astype(float))
    residual = displacement - expected
    usable = np.flatnonzero(~np.isnan(residual))
    events: list[dict[str, Any]] = []
    supported: list[list[float]] = []
    noise = threshold = None
    if len(usable) >= PARAMETERS["minimumExpectedCurveHits"]:
        hit_times, hit_residuals, hit_weights = times[usable], residual[usable], agreeing[usable].astype(float)
        # The quietest third sets the noise, so a disturbed passage cannot raise its own threshold.
        noise = float(np.percentile(np.abs(hit_residuals), 30) / 0.3853)
        threshold = max(PARAMETERS["minimumDeviationSeconds"], PARAMETERS["deviationNoiseMultiple"] * noise)
        centres = np.arange(0, duration + 1e-9, 0.1)
        half = PARAMETERS["supportWindowSeconds"] / 2
        inside = (np.searchsorted(hit_times, centres + half, "right") - np.searchsorted(hit_times, centres - half, "left"))
        supported = _ranges(inside >= PARAMETERS["minimumHitsPerSupportWindow"], centres, half, duration)
        # Evidence is graded: half the threshold is neutral, on-time hits count against a disturbance,
        # and a link too far away to be the same hit counts for nothing.
        deviation = np.abs(hit_residuals) / threshold
        stray = np.abs(hit_residuals) > PARAMETERS["maximumDeviationSeconds"]
        scores = np.where(stray, 0.0, hit_weights / float(np.median(hit_weights)) * np.clip(2 * deviation - 1, -1, 1))
        claimed = np.zeros(len(usable), dtype=bool)
        for first, last, score in _evidence_runs(hit_times, scores):
            deviant = (deviation[first:last + 1] >= 1) & ~stray[first:last + 1]
            span = float(hit_times[last] - hit_times[first])
            # Thin evidence strewn over a long, poorly referenced passage is not a disturbance.
            if (score < PARAMETERS["minimumEvidenceScore"] or deviant.sum() < PARAMETERS["minimumDeviantHits"]
                    or span < PARAMETERS["minimumDisturbanceSeconds"]
                    or score < PARAMETERS["minimumEvidencePerSecond"] * span):
                continue
            claimed[first:last + 1] = True
            members = usable[first:last + 1]
            absolute = np.abs(hit_residuals[first:last + 1])[~stray[first:last + 1]]
            events.append(_event("repeated_hit_timing_shift_suspected", "warning",
                max(0.0, float(hit_times[first]) - PARAMETERS["eventMarginSeconds"]),
                min(duration, float(hit_times[last]) + PARAMETERS["eventMarginSeconds"]),
                score / (3 * PARAMETERS["minimumEvidenceScore"]), "intent_unknown",
                {"mode": "repetition_displacement", "measuredHitCount": int(last - first + 1),
                 "deviantHitCount": int(deviant.sum()), "strayHitCount": int(stray[first:last + 1].sum()),
                 "evidenceScore": round(score, 3),
                 "peakDeviationMilliseconds": round(float(np.max(absolute)) * 1000, 2),
                 "timingDeviationP95Milliseconds": round(float(np.percentile(absolute, 95)) * 1000, 2),
                 "medianDeviationMilliseconds": round(float(np.median(absolute)) * 1000, 2),
                 "measuredOnsetTimesSeconds": np.round(times[members], 4).tolist(),
                 "expectedTimesSeconds": np.round(times[members] - residual[members], 4).tolist(),
                 "expectedTimingKind": EXPECTED_TIMING_KIND, "intent": "unknown"},
                {"minimumDeviationMilliseconds": round(threshold * 1000, 2),
                 "maximumDeviationMilliseconds": PARAMETERS["maximumDeviationSeconds"] * 1000,
                 "minimumEvidenceScore": PARAMETERS["minimumEvidenceScore"],
                 "minimumEvidencePerSecond": PARAMETERS["minimumEvidencePerSecond"],
                 "minimumDeviantHits": PARAMETERS["minimumDeviantHits"],
                 "minimumDurationSeconds": PARAMETERS["minimumDisturbanceSeconds"]},
                "Several consecutive hits arrive early or late compared with the same hits in other repetitions; "
                "listen to tell an unintended timing disturbance from a deliberate variation."))
        # A gradual, sustained departure is reported as tempo, never as a disturbance.
        smooth = np.abs(expected[usable]) >= PARAMETERS["minimumSmoothChangeSeconds"]
        edges = np.flatnonzero(np.diff(np.r_[False, smooth & ~claimed, False].astype(np.int8)))
        for first, stop in zip(edges[::2], edges[1::2]):
            start, end = float(hit_times[first]), float(hit_times[stop - 1])
            if end - start < PARAMETERS["minimumSmoothChangeDurationSeconds"]:
                continue
            curve = expected[usable[first:stop]]
            events.append(_event("smooth_timing_change_observed", "info", start, end, 0.5, "observed",
                {"mode": "tempo_drift", "measuredHitCount": int(stop - first),
                 "peakExpectedDisplacementMilliseconds": round(float(np.max(np.abs(curve))) * 1000, 2),
                 "expectedTimingKind": EXPECTED_TIMING_KIND, "intent": "unknown"},
                {"minimumExpectedDisplacementMilliseconds": PARAMETERS["minimumSmoothChangeSeconds"] * 1000,
                 "minimumDurationSeconds": PARAMETERS["minimumSmoothChangeDurationSeconds"]},
                "Hit timing departs gradually from its repetitions and returns; this fits a gradual tempo "
                "change rather than a disturbance, without confirming that it was intended."))
    if path.stat().st_size != source_bytes or sha256_file(path) != source_hash:
        raise ValueError("Audio changed during timbre timing analysis")
    events.sort(key=lambda event: (event["startSeconds"], event["check"]))
    supported_seconds = sum(end - start for start, end in supported)
    warned = any(event["severity"] == "warning" for event in events)
    status = "needs_review" if warned else "observed" if supported else "unknown"
    reason = ("Hit timing was measured against the track's own repetitions; unsupported ranges remain unknown."
              if supported else "No repeated rhythm with enough agreeing references was found; timing is not judged."
              if lags else "No repeated rhythm was found to serve as a reference; timing is not judged.")
    points = [[round(float(times[index]), 4), round(float(times[index] - residual[index]), 4),
               round(float(residual[index]), 4), round(float(displacement[index]), 4),
               int(agreeing[index]), int(linked[index])] for index in usable]
    return {"version": VERSION, "sourceArtifactSha256": source_hash, "sourceAudioBytes": source_bytes,
        "durationSeconds": duration, "modelInference": False, "modelPredictedBeatTimesSeconds": [],
        "measurementKind": "PCM_spectral_attack_peaks", "expectedTimingKind": EXPECTED_TIMING_KIND,
        "analysisResolutionSeconds": hop, "analysisWindowSeconds": window,
        "timeMeaning": "centered-window spectral attack peak, not sample-exact physical impact time",
        "parameters": dict(PARAMETERS),
        "repetitionLagsSeconds": [round(lag * hop, 4) for lag in lags],
        "repetitionStrengths": [round(value, 4) for value in strengths],
        "referenceCount": len(reference_lags),
        "measuredOnsetTimesSeconds": np.round(times, 4).tolist(), "measuredAttackCount": count,
        "linkedHitCount": int(len(usable)),
        "hits": {"columns": HIT_COLUMNS, "points": points},
        "noiseSeconds": None if noise is None else round(noise, 5),
        "deviationThresholdSeconds": None if threshold is None else round(threshold, 5),
        "supportedRangesSeconds": supported,
        "supportedFraction": round(min(1.0, supported_seconds / duration), 4),
        "check": {"status": status, "reason": reason, "retryEligible": False,
                  "source": {"kind": "full_mix_pcm", "method": "hits linked to their own repetitions",
                             "reliability": 0.8, "sourceArtifactSha256": source_hash},
                  "observed": {"measuredHitCount": count, "linkedHitCount": int(len(usable)),
                               "referenceCount": len(reference_lags),
                               "supportedFraction": round(min(1.0, supported_seconds / duration), 4),
                               "noiseMilliseconds": None if noise is None else round(noise * 1000, 2),
                               "deviationThresholdMilliseconds": None if threshold is None else round(threshold * 1000, 2)}},
        "events": events,
        "confidenceMeaning": "strength of agreeing repetition evidence, not probability of an unintended defect",
        "limitations": [
            "Timing is judged only where the rhythm repeats; other ranges are unknown, not approved.",
            "A matching attack shape supports a same-timbre hypothesis, not a verified instrument.",
            "One or two displaced hits, and departures slower than about three seconds, are not reported as disturbances.",
            "A change repeated identically in every repetition is the reference itself and cannot be detected.",
            "Expected times come from a smooth fit; they are predictions, not measurements or model beats."]}
