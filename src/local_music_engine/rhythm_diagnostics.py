"""Conservative, model-free diagnosis from explicitly attributed audio features.

Percussive pulse timing and separated accompaniment continuity are distinct
observations. Neither is proof of an unintended musical defect. This module
loads no audio or models, changes no audio, and never authorizes regeneration.
"""

from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any

import numpy as np

VERSION = "rhythm-diagnostics-v4"
SIGNAL_VERSION = "rhythm-diagnostics-v3"
PREVIOUS_VERSION = "rhythm-diagnostics-v2"
LEGACY_VERSION = "rhythm-diagnostics-v1"
SUPPORTED_VERSIONS = (LEGACY_VERSION, PREVIOUS_VERSION, SIGNAL_VERSION, VERSION)
MAX_SECONDS = 610.0
MAX_POINTS = 122_000
MAX_ONSETS = 30_000
MAX_EVENTS = 64
MIN_SOURCE_RELIABILITY = 0.5
MIN_PULSE_CONFIDENCE = 0.6
TIMING_KINDS = {"isolated_percussion", "percussive_estimate", "synthetic_percussion"}
BACKING_KINDS = {"separated_accompaniment", "separated_instrumental", "synthetic_accompaniment"}
VOCAL_KINDS = {"separated_vocal", "synthetic_vocal"}
VARIABLE_TIMING = re.compile(r"rubato|accelerando|ritardando|tempo\s*chang|free\s*time|루바토|변박|템포\s*변|자유로운\s*박", re.I)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(value) else None


def validate_expected_sections(sections: Any, duration_seconds: float) -> list[dict[str, Any]]:
    """Validate explicit local plans; a genre or section name cannot excuse an event.

    These are declarations, not human listening decisions. In particular,
    ``rubato`` and ``tempo_change`` can explain a measured tempo departure,
    but neither automatically excuses arbitrary onset jitter.
    """
    duration = _number(duration_seconds)
    if duration is None or not 0 < duration <= MAX_SECONDS:
        raise ValueError("Intent duration must be finite and within the supported audio duration")
    if sections is None:
        return []
    if not isinstance(sections, list) or len(sections) > 128:
        raise ValueError("Intent sections must be a list of at most 128 local ranges")
    allowed = {"startSeconds", "endSeconds", "name", "expectedRest", "accompanimentExpected", "timingIntent"}
    normalized = []
    for item in sections:
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError("Intent section contains unsupported fields")
        start, end = _number(item.get("startSeconds")), _number(item.get("endSeconds"))
        if start is None or end is None or not 0 <= start < end <= duration:
            raise ValueError("Intent section requires a finite local range within the measured audio")
        section: dict[str, Any] = {"startSeconds": start, "endSeconds": end}
        if "name" in item:
            if not isinstance(item["name"], str) or len(item["name"]) > 240:
                raise ValueError("Intent section name must be text of at most 240 characters")
            section["name"] = item["name"]
        for key in ("expectedRest", "accompanimentExpected"):
            if key in item:
                if not isinstance(item[key], bool):
                    raise ValueError(f"{key} must be a boolean")
                section[key] = item[key]
        if "timingIntent" in item:
            if item["timingIntent"] not in ("rubato", "tempo_change"):
                raise ValueError("timingIntent must be rubato or tempo_change")
            section["timingIntent"] = item["timingIntent"]
        normalized.append(section)
    return sorted(normalized, key=lambda section: (section["startSeconds"], section["endSeconds"]))


def _covering_section(event: dict[str, Any], sections: list[dict[str, Any]],
                      *, timing: bool = False) -> dict[str, Any] | None:
    for section in sections:
        applicable = (section.get("timingIntent") in ("rubato", "tempo_change") if timing else
                      section.get("expectedRest") is True or section.get("accompanimentExpected") is False)
        # Numerical rounding tolerance only. A plan overlapping part of a
        # finding must not conceal an unexplained departure outside the plan.
        if applicable and section["startSeconds"] - 0.0001 <= event["startSeconds"] and event["endSeconds"] <= section["endSeconds"] + 0.0001:
            return section
    return None


def contextualize_timing_events(check: dict[str, Any], events: list[dict[str, Any]],
                                sections: list[dict[str, Any]]) -> None:
    """Compare timing events with declared local tempo plans, in place.

    A plan can explain a gradual tempo departure. It never excuses irregular
    timing, and it does not confirm that either was intended.
    """
    for event in events:
        if event["category"] != "beat_timing":
            continue
        section = _covering_section(event, sections, timing=True)
        if section is None:
            continue
        event["observed"]["declaredTimingIntent"] = section["timingIntent"]
        event["observed"]["intentRangeSeconds"] = [section["startSeconds"], section["endSeconds"]]
        if event["observed"].get("mode") == "tempo_drift":
            event["severity"] = "info"
            event["observed"]["intentAssessment"] = "consistent_with_local_tempo_plan"
            event["message"] = "Measured tempo departure fits a declared local tempo plan; this does not confirm musical quality or exclude another defect in the same range."
        else:
            event["observed"]["intentAssessment"] = "irregular_timing_not_explained_by_tempo_plan"
    if check["status"] == "needs_review" and not any(event["severity"] == "warning" for event in events):
        observed = check["observed"]
        check["status"] = "unknown" if observed.get("wholeTrackPulseSupported") is False else "observed"
    check["observed"]["localIntentMatchedCount"] = sum(
        event["observed"].get("intentAssessment") == "consistent_with_local_tempo_plan" for event in events)


def contextualize_tempo_observations(rhythm: dict[str, Any], sections: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the same local plan to broad tempo drift, preserving measurements.

    Requested-BPM differences, spectral shifts and unrelated findings remain
    independent observations. A partially covered full-track drift remains a
    warning even when one local detailed observation has an explanation.
    """
    result = deepcopy(rhythm)
    for finding in result.get("findings", []):
        if finding.get("check") not in {"tempo_drift", "tempo_mismatch"}:
            continue
        # The first-stage v1 observer can mark these informational based on
        # whole-track style words. V2 restores their review status before
        # applying a specific matching plan, including for "no rubato".
        if finding.get("severity") == "info":
            finding["severity"] = "warning"
        if finding.get("check") == "tempo_mismatch":
            continue
        start, end = _number(finding.get("startSeconds")), _number(finding.get("endSeconds"))
        if start is None or end is None or start >= end:
            continue
        section = _covering_section(finding, sections, timing=True)
        if section is None:
            continue
        finding["severity"] = "info"
        finding.setdefault("observed", {}).update(declaredTimingIntent=section["timingIntent"],
            intentRangeSeconds=[section["startSeconds"], section["endSeconds"]],
            intentAssessment="consistent_with_local_tempo_plan", intent="unknown")
        finding["message"] = "Local pulse estimates vary inside a declared tempo plan; this is a compatible observation, not a confirmed musical error or listening approval."
    if any(finding.get("severity") in {"warning", "error"} for finding in result.get("findings", [])):
        result["status"] = "needs_review"
    elif result.get("status") == "needs_review":
        result["status"] = "observed" if _number(result.get("estimatedBpm")) else "unknown"
    return result


def _source(data: dict[str, Any] | None, kinds: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(data, dict) or not isinstance(data.get("source"), dict):
        return None, "Explicit feature-source provenance is unavailable."
    supplied = data["source"]
    reliability = _number(supplied.get("reliability"))
    source = {"kind": str(supplied.get("kind", "unknown"))[:80],
              "method": str(supplied.get("method", "unknown"))[:160],
              "reliability": reliability}
    for key in ("provenanceStatus", "validation", "evidenceStatus"):
        if isinstance(supplied.get(key), str):
            source[key] = supplied[key][:240]
    if source["kind"] not in kinds:
        return source, "This source is a proxy for a different component and cannot support this diagnosis."
    if reliability is None or not MIN_SOURCE_RELIABILITY <= reliability <= 1:
        return source, "Source reliability is unavailable or insufficient for this diagnosis."
    return source, None


def _unknown(reason: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason, "source": source, "observed": {}}


def _times(values: Any, duration: float) -> np.ndarray | None:
    if not isinstance(values, (list, tuple, np.ndarray)) or len(values) > MAX_ONSETS:
        return None
    parsed = [_number(value) for value in values]
    if any(value is None or not 0 <= value <= duration for value in parsed):
        return None
    result = np.asarray(parsed, dtype=float)
    if len(result) > 1 and np.any(np.diff(result) <= 0):
        return None
    return result


def _event(check: str, category: str, start: float, end: float, confidence: float,
           observed: dict[str, Any], threshold: dict[str, Any], message: str,
           *, severity: str = "warning", evidence_status: str = "suspected") -> dict[str, Any]:
    return {"check": check, "category": category, "severity": severity,
            "confidence": _rounded(min(1.0, max(0.0, confidence))),
            "startSeconds": _rounded(start), "endSeconds": _rounded(end),
            "evidenceStatus": evidence_status, "observed": observed,
            "threshold": threshold, "message": message, "retryEligible": False}


def _density(onsets: np.ndarray, start: float, end: float, period: float) -> float:
    return float(np.count_nonzero((onsets >= start) & (onsets < end))) * period / max(end - start, period)


def _group(indices: np.ndarray, bridge: int = 0) -> list[tuple[int, int]]:
    if not len(indices):
        return []
    groups: list[tuple[int, int]] = []
    first = previous = int(indices[0])
    for item in indices[1:]:
        index = int(item)
        if index - previous > bridge + 1:
            groups.append((first, previous))
            first = index
        previous = index
    groups.append((first, previous))
    return groups


def _recover_displaced_onsets(beats: np.ndarray, onsets: np.ndarray, period: float,
                              ranges: list[tuple[float, float]]) -> tuple[np.ndarray, int, list[tuple[float, float]]]:
    """Restore actual late/early peaks excluded by a narrow pulse-phase filter.

    Only interior gaps with stable neighboring observed candidates qualify.
    This never fills a silent slot: a separate measured onset must exist.
    Raw onset density is still checked before any instability finding.
    """
    intervals = np.diff(beats)
    recovered: list[float] = []
    ambiguous: list[tuple[float, float]] = []
    for index in np.flatnonzero(intervals > period * 1.5):
        count = round(float(intervals[index]) / period)
        if not 2 <= count <= 12 or index < 4 or index + 5 >= len(intervals):
            continue
        if abs(float(intervals[index]) - count * period) > period * 0.1:
            continue
        flanks = np.r_[intervals[index - 4:index], intervals[index + 1:index + 5]]
        if np.max(np.abs(flanks - period)) > max(0.025, period * 0.06):
            continue
        if not any(start <= beats[index] < beats[index + 1] <= end for start, end in ranges):
            continue
        available = onsets[(onsets > beats[index] + period * 0.4) & (onsets < beats[index + 1] - period * 0.4)]
        if not len(available):
            continue
        before_density = _density(onsets, beats[index - 4], beats[index], period)
        after_density = _density(onsets, beats[index + 1], beats[index + 5], period)
        local_density = _density(onsets, beats[index], beats[index + 1], period)
        density_ratios = (local_density / max(before_density, 0.1), local_density / max(after_density, 0.1))
        if min(density_ratios) < 0.65 or max(density_ratios) > 1.5:
            ambiguous.append((float(beats[index]), float(beats[index + 1])))
            continue
        local_recovered: list[float] = []
        contested = False
        for slot in range(1, count):
            expected = beats[index] + slot * period
            nearby = np.flatnonzero(np.abs(available - expected) <= period * 0.35)
            if len(nearby) > 1:
                contested = True
                break
            if len(nearby) == 1:
                closest = int(nearby[0])
                local_recovered.append(float(available[closest]))
                available = np.delete(available, closest)
        if contested:
            ambiguous.append((float(beats[index]), float(beats[index + 1])))
        else:
            recovered.extend(local_recovered)
    return np.sort(np.r_[beats, recovered]), len(recovered), ambiguous


def _local_timing(data: dict[str, Any], source: dict[str, Any], duration: float,
                  style: str) -> tuple[dict[str, Any], list[dict[str, Any]], np.ndarray, None]:
    """Use adjacent independently supported regions without trusting a weak whole-track pulse."""
    segments = data.get("segments")
    if not isinstance(segments, list) or len(segments) > 128:
        return _unknown("An independently estimated percussive pulse is not sufficiently supported.", source), [], np.empty(0), None
    supported = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        start, end = _number(item.get("startSeconds")), _number(item.get("endSeconds"))
        bpm, confidence = _number(item.get("bpm", item.get("estimatedBpm"))), _number(item.get("confidence"))
        if start is not None and end is not None and bpm is not None and confidence is not None and 0 <= start < end <= duration and 30 <= bpm <= 300 and MIN_PULSE_CONFIDENCE <= confidence <= 1:
            supported.append((start, end, bpm, confidence))
    supported.sort()
    groups: list[list[tuple[float, float, float, float]]] = []
    for item in supported:
        if (groups and abs(item[0] - groups[-1][-1][1]) <= 0.0001
                and abs(item[2] / groups[-1][-1][2] - 1) <= 0.05
                and abs(item[2] / groups[-1][0][2] - 1) <= 0.05):
            groups[-1].append(item)
        else:
            groups.append([item])
    events: list[dict[str, Any]] = []
    observations = []
    original_ranges = _ranges(data, duration)
    observed_beats: list[float] = []
    for group in groups:
        if len(group) < 2:
            continue
        start, end = group[0][0], group[-1][1]
        bpm = float(np.median([item[2] for item in group]))
        confidence = min(item[3] for item in group)
        local = dict(data)
        local["pulse"] = {"bpm": bpm, "confidence": confidence}
        local["validatedRanges"] = [[max(start, left), min(end, right)] for left, right in original_ranges if max(start, left) < min(end, right)]
        # _timing accepts this explicitly supported local pulse, so recursion
        # ends here. The original all-onset evidence remains unfiltered.
        check, found, beats, _ = _timing(local, duration, style)
        if not check["observed"]:
            continue
        events.extend(found)
        observed_beats.extend(beats.tolist())
        observations.append({"startSeconds": _rounded(start), "endSeconds": _rounded(end),
                             "estimatedBpm": _rounded(bpm), "pulseConfidence": _rounded(confidence),
                             "status": check["status"], "observedPulseCount": len(beats)})
    if not observations:
        return _unknown("Whole-track pulse evidence is weak and no adjacent supported local candidate tracks are sufficient.", source), [], np.empty(0), None
    check = {"status": "needs_review" if any(event["severity"] == "warning" for event in events) else "unknown",
             "reason": "Only these adjacent local percussion ranges are supported; whole-track timing remains unknown.",
             "source": source, "observed": {"localSupportedRanges": observations, "wholeTrackPulseSupported": False,
             "observedPulseCount": len(set(observed_beats))}}
    # Different local phase references do not define a whole-track grid.
    return check, events, np.asarray(sorted(set(observed_beats))), None


def _timing(data: dict[str, Any] | None, duration: float, style: str) -> tuple[dict[str, Any], list[dict[str, Any]], np.ndarray, float | None]:
    source, error = _source(data, TIMING_KINDS)
    if error:
        return _unknown(error, source), [], np.empty(0), None
    assert data is not None and source is not None
    pulse = data.get("pulse")
    bpm = _number(pulse.get("bpm")) if isinstance(pulse, dict) else None
    pulse_confidence = _number(pulse.get("confidence")) if isinstance(pulse, dict) else None
    beats = _times(data.get("beatCandidatesSeconds"), duration)
    onsets = _times(data.get("onsetTimesSeconds"), duration)
    if bpm is None or not 30 <= bpm <= 300 or pulse_confidence is None or not MIN_PULSE_CONFIDENCE <= pulse_confidence <= 1:
        return _local_timing(data, source, duration, style)
    period = 60 / bpm
    if beats is None or onsets is None:
        return _unknown("Ordered observed percussion pulse candidates and their onset evidence are required.", source), [], np.empty(0), period
    ranges = _ranges(data, duration)
    valid_beats = np.zeros(len(beats), dtype=bool)
    valid_onsets = np.zeros(len(onsets), dtype=bool)
    for start, end in ranges:
        valid_beats |= (beats >= start) & (beats <= end)
        valid_onsets |= (onsets >= start) & (onsets <= end)
    beats, onsets = beats[valid_beats], onsets[valid_onsets]
    source["validatedRanges"] = [[_rounded(start), _rounded(end)] for start, end in ranges]
    if len(beats) < 24 or len(onsets) < len(beats):
        return _unknown("At least 24 ordered observed percussion pulse candidates and their onset evidence are required.", source), [], np.empty(0), period
    # A synthetic metronome grid must not be submitted as observed candidates.
    # Every candidate must have an actual source onset within its localization
    # tolerance; missing beats never become invented timing observations.
    closest = np.searchsorted(onsets, beats)
    distances = np.minimum(np.abs(beats - onsets[np.clip(closest, 0, len(onsets) - 1)]),
                           np.abs(beats - onsets[np.clip(closest - 1, 0, len(onsets) - 1)]))
    if np.mean(distances <= min(0.025, period * 0.06)) < 0.95:
        return _unknown("Pulse candidates are not corroborated by observed percussion onsets.", source), [], beats, period
    beats, recovered_count, ambiguous_gaps = _recover_displaced_onsets(beats, onsets, period, ranges)
    intervals = np.diff(beats)
    missing = np.rint(intervals / period).astype(int) - 1
    validated_intervals = np.zeros(len(intervals), dtype=bool)
    for start, end in ranges:
        validated_intervals |= (beats[:-1] >= start) & (beats[1:] <= end)
    ordinary = validated_intervals & (intervals >= period * 0.4) & (intervals <= period * 1.6)
    if np.count_nonzero(ordinary) < 20:
        return _unknown("The candidate track is too sparse or changes metrical level; timing cannot be compared reliably.", source), [], beats, period
    confidence = min(float(source["reliability"]), pulse_confidence)
    variable_intent = bool(VARIABLE_TIMING.search(style))
    events: list[dict[str, Any]] = []
    # Omitted pulses are a separate observation, not evidence of tempo jitter
    # or loss of the entire accompaniment. Intro/outro absence is excluded.
    for index in np.flatnonzero(validated_intervals & (intervals >= period * 1.75)):
        if any(start <= beats[index] and beats[index + 1] <= end for start, end in ambiguous_gaps):
            continue
        if index < 8 or index + 8 >= len(intervals):
            continue
        if not np.all(ordinary[index - 8:index]) or not np.all(ordinary[index + 1:index + 9]):
            continue
        events.append(_event("percussive_gap_observed", "percussion_gap", beats[index] + period / 2,
            beats[index + 1] - period / 2, confidence,
            {"missingPulseCount": max(1, int(missing[index])), "gapSeconds": _rounded(intervals[index]),
             "estimatedPeriodSeconds": _rounded(period), "intent": "unknown"},
            {"minimumGapPeriods": 1.75},
            "Observed percussion pulses are absent inside a previously supported pulse; an intentional drum rest can explain this gap.",
            severity="info", evidence_status="observed"))
    # Detect local departures from a stable neighboring pulse. Irregular whole
    # tracks, syncopation and swing have no stable comparison and stay unknown.
    residual = np.abs(intervals - period)
    anomalous = np.flatnonzero(ordinary & (residual >= max(0.03, period * 0.08)))
    comparisons = 0
    excluded_density = 0
    for first, last in _group(anomalous, bridge=2):
        if last - first < 1 or first < 8 or last + 9 >= len(intervals):
            continue
        local = intervals[first:last + 1]
        left, right = intervals[first - 8:first], intervals[last + 1:last + 9]
        if not np.all(ordinary[first - 8:last + 9]):
            continue
        left_period, right_period = float(np.median(left)), float(np.median(right))
        reference = (left_period + right_period) / 2
        if max(abs(left_period - period), abs(right_period - period)) > period * 0.05:
            continue
        if max(float(np.percentile(np.abs(left - left_period), 90)), float(np.percentile(np.abs(right - right_period), 90))) > max(0.018, period * 0.04):
            continue
        if abs(left_period - right_period) > period * 0.05:
            continue
        before_density = _density(onsets, beats[first - 8], beats[first], reference)
        after_density = _density(onsets, beats[last + 1], beats[last + 9], reference)
        local_density = _density(onsets, beats[first], beats[last + 1], reference)
        ratios = (local_density / max(before_density, 0.1), local_density / max(after_density, 0.1))
        if min(ratios) < 0.65 or max(ratios) > 1.5:
            excluded_density += 1
            continue
        comparisons += 1
        local_period = float(np.median(local))
        deviations = np.abs(local - reference)
        affected = int(np.count_nonzero(deviations >= max(0.03, reference * 0.08)))
        coherent_single_shift = (len(local) == 2 and (local[0] - reference) * (local[1] - reference) < 0
                                 and abs(float(np.sum(local)) - 2 * reference) < reference * 0.06)
        if affected < 3 and not (affected == 2 and coherent_single_shift):
            continue
        mad = float(np.median(np.abs(local - local_period)))
        period_change = abs(local_period - reference) / reference
        mode = "tempo_drift" if period_change >= 0.08 and mad < reference * 0.05 else "jitter"
        events.append(_event("beat_timing_instability_suspected", "beat_timing", beats[first], beats[last + 1], confidence,
            {"mode": mode, "referencePeriodSeconds": _rounded(reference), "localPeriodSeconds": _rounded(local_period),
             "periodChangePercent": _rounded(period_change * 100, 2), "intervalMadMilliseconds": _rounded(mad * 1000, 3),
             "timingDeviationP95Milliseconds": _rounded(float(np.percentile(deviations, 95)) * 1000, 3),
             "onsetDensityRatioBefore": _rounded(ratios[0]), "onsetDensityRatioAfter": _rounded(ratios[1]),
             "observedPulseCount": int(len(local) + 1), "missingPulseCount": 0, "intent": "unknown"},
            {"minimumTimingDeviationMilliseconds": max(30, round(reference * 80, 3)),
             "minimumAffectedIntervals": 2 if coherent_single_shift else 3, "maximumOnsetDensityRatio": 1.5},
            "Percussive pulse timing departs from stable neighboring pulse candidates; listen to distinguish unintended timing from deliberate groove or tempo changes.",
            severity="info" if variable_intent else "warning", evidence_status="intent_unknown"))
    events.extend(_tempo_changes(data, beats, onsets, source, pulse_confidence, duration, variable_intent, events))
    normal_count = int(np.count_nonzero(residual[ordinary] < max(0.018, period * 0.04)))
    supported = (normal_count >= 16 and not ambiguous_gaps) or any(event["category"] == "beat_timing" for event in events)
    check = {"status": "needs_review" if any(event["severity"] == "warning" for event in events) else "observed" if supported else "unknown",
             "reason": "Observed percussion pulse comparisons; absence of a warning does not verify meter or musical quality." if supported else "No stable neighboring percussive reference is available for the irregular candidate track.",
             "source": source, "observed": {"estimatedBpm": _rounded(bpm), "pulseConfidence": _rounded(pulse_confidence),
             "observedPulseCount": len(beats), "stableIntervalCount": normal_count,
             "recoveredObservedOnsetCount": recovered_count,
             "countRecoveredOnsets": recovered_count, "ambiguousPulseGapCount": len(ambiguous_gaps),
             "candidateRecoveryMethod": "unique measured onset within 35% of an independently supported pulse, with stable flanks and matching onset density",
             "missingPulseCount": int(np.sum(np.maximum(0, missing[validated_intervals]))), "localComparisonCount": comparisons,
             "excludedOnsetDensityChanges": excluded_density}}
    return check, events, beats, period


def _tempo_changes(data: dict[str, Any], beats: np.ndarray, onsets: np.ndarray,
                   source: dict[str, Any], pulse_confidence: float, duration: float,
                   variable_intent: bool, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments = data.get("segments", [])
    if not isinstance(segments, list) or len(segments) > 128:
        return []
    supported: list[tuple[float, float, float, float, float]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        start, end = _number(segment.get("startSeconds")), _number(segment.get("endSeconds"))
        bpm = _number(segment.get("bpm", segment.get("estimatedBpm")))
        confidence = _number(segment.get("confidence"))
        if start is None or end is None or bpm is None or confidence is None or not 0 <= start < end <= duration or not 30 <= bpm <= 300 or not 0.65 <= confidence <= 1:
            continue
        local = beats[(beats >= start) & (beats < end)]
        period = 60 / bpm
        if len(local) < 12 or end - start < period * 8:
            continue
        intervals = np.diff(local)
        # Independent local tempo must agree with actual candidate intervals,
        # rather than just another autocorrelation peak or skipped pulse rate.
        if np.mean(np.abs(intervals - period) <= period * 0.06) < 0.8:
            continue
        supported.append((start, end, bpm, confidence, _density(onsets, start, end, period)))
    supported.sort()
    results: list[dict[str, Any]] = []
    for left, right in zip(supported, supported[1:]):
        if right[0] < left[1] or right[0] - left[1] > max(1.0, 120 / left[2]):
            continue
        ratio = right[2] / left[2]
        # Reject close half/double, compound and triplet pulse aliases.
        if min(abs(ratio - alias) / alias for alias in (0.5, 2 / 3, 1.5, 2.0, 3.0)) <= 0.05:
            continue
        if abs(ratio - 1) <= 0.08 or not 0.65 <= right[4] / max(left[4], 0.1) <= 1.5:
            continue
        if any(event["category"] == "beat_timing" and event["startSeconds"] <= right[0] <= event["endSeconds"] for event in existing):
            continue
        results.append(_event("beat_timing_instability_suspected", "beat_timing", left[0], right[1],
            min(source["reliability"], pulse_confidence, left[3], right[3]),
            {"mode": "tempo_drift", "beforeBpm": _rounded(left[2]), "afterBpm": _rounded(right[2]),
             "periodChangePercent": _rounded(abs(1 / ratio - 1) * 100, 2),
             "onsetDensityRatioAfter": _rounded(right[4] / max(left[4], 0.1)), "intent": "unknown"},
            {"minimumTempoChangePercent": 8, "minimumLocalPulseCount": 12},
            "Consecutive independently supported percussion segments have different pulse rates; intentional tempo changes remain possible.",
            severity="info" if variable_intent else "warning", evidence_status="intent_unknown"))
    return results


def _ranges(data: dict[str, Any], duration: float) -> list[tuple[float, float]]:
    source = data.get("source", {})
    supplied = data.get("validatedRanges", source.get("validatedRanges"))
    if supplied is None:
        pair = data.get("validRangeSeconds", source.get("validRangeSeconds"))
        supplied = [pair] if pair is not None else [[0, duration]]
    if not isinstance(supplied, list) or len(supplied) > 128:
        return []
    ranges = []
    for pair in supplied:
        if isinstance(pair, dict):
            pair = [pair.get("startSeconds"), pair.get("endSeconds")]
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, end = _number(pair[0]), _number(pair[1])
        if start is not None and end is not None and 0 <= start < end <= duration:
            ranges.append((start, end))
    return ranges


def _series(data: dict[str, Any], duration: float) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, float] | None:
    series = data.get("frameSeries")
    if not isinstance(series, dict):
        return None
    columns, points = series.get("columns"), series.get("points")
    if not isinstance(columns, list) or not isinstance(points, (list, np.ndarray)) or not 12 <= len(points) <= MAX_POINTS:
        return None
    if "timeSeconds" not in columns or "rmsDbfs" not in columns:
        return None
    ti, ri = columns.index("timeSeconds"), columns.index("rmsDbfs")
    mi = columns.index("mixRmsDbfs") if "mixRmsDbfs" in columns else None
    rows = []
    for row in points:
        if not isinstance(row, (list, tuple, np.ndarray)) or len(row) != len(columns):
            return None
        time = _number(row[ti])
        rms = -120.0 if row[ri] is None else _number(row[ri])
        mix = (-120.0 if row[mi] is None else _number(row[mi])) if mi is not None else 0.0
        if time is None or rms is None or mix is None or not 0 <= time <= duration or not -300 <= rms <= 20 or not -300 <= mix <= 20:
            return None
        rows.append((time, rms, mix))
    array = np.asarray(rows, dtype=float)
    increments = np.diff(array[:, 0])
    declared_step = _number(data.get("windowSeconds"))
    step = declared_step if declared_step is not None else float(np.median(increments))
    if step <= 0 or step > 0.5 or np.any(increments <= 0):
        return None
    ranges: list[tuple[float, float]] = []
    for start, end in sorted(_ranges(data, duration)):
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    tolerance = max(1e-6, step * 0.001)
    gaps = np.flatnonzero(increments > step * 1.5)
    if len(gaps):
        # Cached revisions omit windows whose PCM no longer matches. Restore
        # only their timestamps, with unavailable values, so an unrelated gap
        # cannot discard supported comparisons elsewhere in the track.
        # Full support, an irregular jump, or an unexplained missing window
        # must still fail instead of becoming invented silence or interpolation.
        source = data.get("source", {})
        if not any(data.get(key) is not None or source.get(key) is not None
                   for key in ("validatedRanges", "validRangeSeconds")):
            return None
        pieces, previous, count = [], 0, len(array)
        for index in gaps:
            ratio = float(increments[index]) / step
            if not math.isfinite(ratio) or ratio > MAX_POINTS:
                return None
            slots = round(ratio)
            if abs(float(increments[index]) - slots * step) > tolerance:
                return None
            count += slots - 1
            if count > MAX_POINTS:
                return None
            missing_times = array[index, 0] + np.arange(1, slots) * step
            for start, end in ranges:
                if np.any((missing_times >= start) & (missing_times <= end)):
                    return None
            missing_rows = np.full((len(missing_times), 3), np.nan)
            missing_rows[:, 0] = missing_times
            pieces.extend((array[previous:index + 1], missing_rows))
            previous = index + 1
        pieces.append(array[previous:])
        array = np.concatenate(pieces)
    valid = np.zeros(len(array), dtype=bool)
    window_starts = np.maximum(0, array[:, 0] - step / 2)
    window_ends = np.minimum(duration, array[:, 0] + step / 2)
    for start, end in ranges:
        # A gap can fall between frame centers. Its overlapping RMS windows
        # must remain unavailable too, so vocal interpolation cannot bridge it.
        valid |= (window_starts >= start - tolerance) & (window_ends <= end + tolerance)
    # Invalid ranges remain NaN internally and never appear in serialized data.
    array[~valid, 1:] = np.nan
    return array[:, 0], array[:, 1], array[:, 2] if mi is not None else None, step


def _grid_relation(start: float, end: float, beats: np.ndarray, period: float | None) -> dict[str, Any]:
    if period is None or len(beats) < 24:
        return {"status": "unknown", "startDistanceSeconds": None, "endDistanceSeconds": None}
    a, b = float(np.min(np.abs(beats - start))), float(np.min(np.abs(beats - end)))
    return {"status": "near_inferred_pulse" if max(a, b) <= min(0.08, period * 0.12) else "off_inferred_pulse",
            "startDistanceSeconds": _rounded(a), "endDistanceSeconds": _rounded(b)}


def _arrangement_region(start: float, end: float, expected_sections: Any, *, tolerance: float = 0.0001) -> bool:
    if not isinstance(expected_sections, list) or len(expected_sections) > 128:
        return False
    for section in expected_sections:
        if not isinstance(section, dict):
            continue
        # A verse/chorus boundary or proximity to a grid alone is not evidence
        # of an intentional rest. Require explicit declared instrumentation.
        if section.get("accompanimentExpected") is not False and section.get("expectedRest") is not True:
            continue
        a, b = _number(section.get("startSeconds")), _number(section.get("endSeconds"))
        if a is not None and b is not None and a - tolerance <= start and end <= b + tolerance:
            return True
    return False


def _backing(data: dict[str, Any] | None, vocal: dict[str, Any] | None, duration: float,
             beats: np.ndarray, period: float | None, expected_sections: Any,
             *, legacy_intent: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source, error = _source(data, BACKING_KINDS)
    vocal_source, vocal_error = _source(vocal, VOCAL_KINDS)
    if error:
        return _unknown(error, source), []
    if vocal_error:
        return _unknown("Independent separated vocal evidence is unavailable: " + vocal_error, source), []
    assert data is not None and vocal is not None and source is not None and vocal_source is not None
    backing_series, vocal_series = _series(data, duration), _series(vocal, duration)
    if backing_series is None or vocal_series is None:
        return _unknown("Timestamped, sufficiently resolved and validated backing/vocal RMS windows are required.", source), []
    times, rms, mix, step = backing_series
    vocal_times, vocal_rms, _, vocal_step = vocal_series
    # Interpolation is allowed only within measured support; it must not bridge
    # a stem-validity gap or invent vocal evidence in an unmeasured intro.
    foreground = np.interp(times, vocal_times, vocal_rms, left=np.nan, right=np.nan)
    flank = max(3, math.ceil(1.0 / step))
    context = max(flank, math.ceil(4.0 / step))
    if len(times) < flank * 4 or step > 0.5 or vocal_step > 0.5:
        return _unknown("Not enough measured stem context remains for two local flanks and a rebound.", source), []
    candidate = np.zeros(len(times), dtype=bool)
    measured = np.isfinite(rms) & np.isfinite(foreground)
    comparison_count = 0
    # Use both surrounding flanks. A one-sided transition, fade, intro or outro
    # cannot satisfy the rebound criterion.
    for index in range(context, len(times) - context):
        left, right = rms[index - context:index], rms[index + 1:index + context + 1]
        if not measured[index] or not np.all(np.isfinite(left)) or not np.all(np.isfinite(right)):
            continue
        before, after = float(np.percentile(left, 75)), float(np.percentile(right, 75))
        if min(before, after) <= -55 or abs(before - after) > 6:
            continue
        comparison_count += 1
        candidate[index] = rms[index] <= min(before, after) - 12
    events: list[dict[str, Any]] = []
    for first, last in _group(np.flatnonzero(candidate), bridge=0):
        # Deep regions longer than a flank are recovered by extending from a
        # supported edge. Extension still needs complete measured context.
        while first > 0 and measured[first - 1] and rms[first - 1] <= rms[first] + 3:
            if times[last] - times[first - 1] > 4:
                break
            first -= 1
        while last + 1 < len(times) and measured[last + 1] and rms[last + 1] <= rms[last] + 3:
            if times[last + 1] - times[first] > 4:
                break
            last += 1
        if first < flank or last + flank + 1 >= len(times):
            continue
        start, end = float(times[first] - step / 2), float(times[last] + step / 2)
        if not 0.2 <= end - start <= 4 or start < 1 or end > duration - 1:
            continue
        before_rows, during, after_rows = rms[first - flank:first], rms[first:last + 1], rms[last + 1:last + flank + 1]
        if not np.all(np.isfinite(np.r_[before_rows, during, after_rows])):
            continue
        before, after, middle = map(float, (np.median(before_rows), np.median(after_rows), np.median(during)))
        depth = min(before, after) - middle
        if depth < 14 or min(before, after) <= -55 or abs(before - after) > 6:
            continue
        if max(float(np.percentile(before_rows, 75) - np.percentile(before_rows, 25)), float(np.percentile(after_rows, 75) - np.percentile(after_rows, 25))) > 6:
            continue
        # Confirm abrupt fall and rebound near each boundary, avoiding smooth
        # dynamic envelopes that merely have a local minimum.
        edge = max(1, math.ceil(0.25 / step))
        if float(np.median(rms[max(0, first - edge):first])) - float(np.median(during[:edge])) < 10:
            continue
        if float(np.median(rms[last + 1:last + 1 + edge])) - float(np.median(during[-edge:])) < 10:
            continue
        foreground_before = foreground[first - flank:first]
        foreground_during = foreground[first:last + 1]
        foreground_after = foreground[last + 1:last + flank + 1]
        if not np.all(np.isfinite(np.r_[foreground_before, foreground_during, foreground_after])):
            continue
        vocal_before, vocal_after = float(np.median(foreground_before)), float(np.median(foreground_after))
        vocal_reference = max(vocal_before, vocal_after)
        active_fraction = float(np.mean(foreground_during >= max(-55, vocal_reference - 9)))
        vocal_continuity = float(np.median(foreground_during)) - vocal_reference
        if active_fraction < 0.75 or vocal_continuity < -9:
            continue
        mix_continuity = None
        if mix is not None:
            if not np.all(np.isfinite(mix[first - flank:last + flank + 1])):
                continue
            mix_reference = min(float(np.median(mix[first - flank:first])), float(np.median(mix[last + 1:last + flank + 1])))
            mix_continuity = float(np.median(mix[first:last + 1])) - mix_reference
            mix_during = float(np.median(mix[first:last + 1]))
            # Losing a loud backing can drop the full mix by far more than
            # 9 dB while a quiet vocal continues. Relative mix level therefore
            # cannot veto independently supported vocal continuity.
            if mix_during <= -60 or mix_during < float(np.median(foreground_during)) - 12:
                continue
        arrangement = _arrangement_region(start, end, expected_sections, tolerance=0.25 if legacy_intent else 0.0001)
        if any(abs(start - event["startSeconds"]) < step * 2 and abs(end - event["endSeconds"]) < step * 2 for event in events):
            continue
        events.append(_event("possible_arrangement_break" if arrangement else "backing_dropout_suspected",
            "arrangement_break" if arrangement else "backing_dropout", max(0.0, start), min(duration, end),
            min(source["reliability"], vocal_source["reliability"]),
            {"relativeDepthDb": _rounded(depth, 2), "dropDurationSeconds": _rounded(end - start),
             "beforeRmsDbfs": _rounded(before, 2), "duringRmsDbfs": _rounded(middle, 2), "afterRmsDbfs": _rounded(after, 2),
             "vocalActiveFraction": _rounded(active_fraction), "vocalContinuityDb": _rounded(vocal_continuity, 2),
             "vocalBeforeRmsDbfs": _rounded(vocal_before, 2), "vocalAfterRmsDbfs": _rounded(vocal_after, 2),
             "mixContinuityDb": _rounded(mix_continuity, 2), "gridRelation": _grid_relation(start, end, beats, period),
             "intent": "unknown", "explicitRestExpected": arrangement},
            {"minimumRelativeDepthDb": 14, "maximumDurationSeconds": 4, "minimumVocalActiveFraction": 0.75,
             "maximumVocalFallDb": 9, "requiresBothFlanksAndRebound": True},
            "Separated accompaniment falls abruptly and rebounds while vocals continue; a declared arrangement rest can explain this observation." if arrangement else
            "Separated accompaniment falls abruptly and rebounds while vocals continue; this may be a dropout or an intentional accompaniment rest.",
            severity="info" if arrangement else "warning", evidence_status="intent_unknown"))
    if comparison_count < 12:
        return _unknown("Insufficient jointly validated active stem context for a continuity comparison.", source), events
    check = {"status": "needs_review" if any(event["severity"] == "warning" for event in events) else "observed",
             "reason": "Separated backing/vocal continuity was measured; intended rests and separation leakage still require listening.",
             "source": source, "vocalSource": vocal_source,
             "observed": {"comparedWindowCount": comparison_count, "windowSeconds": _rounded(step),
                          "dropoutCandidateCount": len(events), "mixContinuityAvailable": mix is not None}}
    return check, events


def diagnose_rhythm(*, duration_seconds: float, percussion: dict[str, Any] | None = None,
                    accompaniment: dict[str, Any] | None = None, vocal: dict[str, Any] | None = None,
                    style_prompt: str = "", expected_sections: Any = None,
                    diagnostics_version: str = VERSION, event_limit: int | None = MAX_EVENTS) -> dict[str, Any]:
    """Diagnose attributed features without interpreting intent or repairing audio.

    Sources require ``kind``, ``method`` and ``reliability`` in ``source``.
    Percussion supplies actual ``onsetTimesSeconds``, ``beatCandidatesSeconds``
    and independent ``pulse: {bpm, confidence}``; optional ``segments`` carry
    local independent pulse estimates. Separated accompaniment/vocal supply
    timestamped ``frameSeries`` with ``timeSeconds``/``rmsDbfs`` columns; backing
    can additionally carry ``mixRmsDbfs``. ``validRangeSeconds`` or
    ``validatedRanges`` limits each source's verified support.

    An HPSS harmonic estimate is not a vocal/backing separator. Missing stem
    evidence yields unknown backing continuity even when timing is observed.
    ``confidence`` denotes strength/source reliability, never defect probability.
    """
    duration = _number(duration_seconds)
    if duration is None or not 0 < duration <= MAX_SECONDS:
        raise ValueError(f"duration_seconds must be finite and in (0, {MAX_SECONDS:g}]")
    if diagnostics_version not in SUPPORTED_VERSIONS:
        raise ValueError("Unsupported rhythm diagnostics version")
    if event_limit is not None and (isinstance(event_limit, bool) or not isinstance(event_limit, int) or not 1 <= event_limit <= 10_000):
        raise ValueError("Diagnostic event limit must be between 1 and 10000 or None")
    legacy = diagnostics_version == LEGACY_VERSION
    sections = expected_sections if legacy else validate_expected_sections(expected_sections, duration)
    style = style_prompt if isinstance(style_prompt, str) else ""
    # Whole-song prompt keywords are context, not a license to hide every
    # anomaly. Preserve that historical interpretation only for frozen v1 plans.
    timing, timing_events, beats, period = _timing(percussion, duration, style[:20_000] if legacy else "")
    if not legacy:
        contextualize_timing_events(timing, timing_events, sections)
    backing, backing_events = _backing(accompaniment, vocal, duration, beats, period, sections, legacy_intent=legacy)
    all_events = timing_events + backing_events
    chronology = lambda event: (event["startSeconds"], event["check"])
    # Display limits must not discard a late warning behind earlier routine
    # observations, or change the outcome of a fully measured check.
    priority = lambda event: (event["severity"] not in {"warning", "error"}, *chronology(event))
    events = sorted(sorted(all_events, key=priority)[:event_limit], key=chronology)
    checks = {"beatTiming": timing, "backingContinuity": backing}
    status = ("needs_review" if any(check["status"] == "needs_review" for check in checks.values())
              or any(event["severity"] in {"warning", "error"} for event in all_events) else
              "unknown" if any(check["status"] == "unknown" for check in checks.values()) else "observed")
    return {"version": diagnostics_version, "status": status, "durationSeconds": _rounded(duration),
            "checks": checks, "events": events, "findings": events,
            "intentPolicy": "legacy_whole_track_keywords" if legacy else "local_declared_plan_with_observation_match",
            "expectedSections": sections if not legacy else [],
            "confidenceMeaning": "Strength of source-attributed observations, not probability of an unintended musical defect.",
            "maximumEvents": event_limit if event_limit is not None else len(events),
            "scope": "observed_percussion_and_separated_backing_features",
            "eventsTruncated": len(all_events) > len(events),
            "totalEventCount": len(all_events),
            "omittedEventCount": len(all_events) - len(events),
            "omittedWarningCount": sum(event["severity"] in {"warning", "error"} for event in all_events)
                - sum(event["severity"] in {"warning", "error"} for event in events),
            "limitations": [
                "Pulse candidates do not verify quarter-note beats, downbeats, meter, swing or musical quality.",
                "Missing percussion pulses and onset-density changes are not treated as timing instability.",
                "HPSS separates transient/harmonic spectral estimates; it does not independently separate vocals and accompaniment.",
                "Separated stem leakage and provenance limit backing-dropout evidence; both flanks and ongoing vocals are required.",
                "Intentional rests, fills and tempo changes remain possible; no event confirms a defect or authorizes a retry or correction.",
                "Local intent plans explain only fully covered compatible observations; a tempo plan does not excuse irregular jitter.",
            ]}
