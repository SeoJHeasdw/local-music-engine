"""Same-source contextual comparisons, without deciding musical intent.

Repeated envelopes and disagreement between estimates are alternate explanations
for review. This module never clears a warning, invents a missing source window,
or treats a spectral transient estimate as an identified instrument.
"""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

import numpy as np

from .rhythm_diagnostics import MAX_POINTS, MAX_SECONDS, _number, _ranges, _series

VERSION = "rhythm-context-v1"
REPEAT_FLANK_SECONDS = 2.0
MIN_REPEAT_MATCHES = 3
MIN_REPEAT_CORRELATION = 0.85
MIN_FLANK_CORRELATION = 0.75
MAX_REPEAT_ERROR_DB = 3.0
MAX_REPEAT_DEPTH_DIFFERENCE_DB = 4.0
MAX_REPEAT_CENTER_DIFFERENCE_DB = 6.0
MAX_REPEAT_EXAMPLES = 8


def _rounded(value: float | None, digits: int = 3) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(value) else None


def _binding(data: dict[str, Any] | None, source_hash: str, *, structure: bool = False) -> str:
    if not isinstance(data, dict):
        return "unavailable"
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    if structure:
        hashes = [data.get("measuredArtifactSha256")]
    else:
        # Native observations are bound to their named source. A cached subset
        # may instead be bound to its explicitly PCM-validated target ranges.
        hashes = [data.get("sourceArtifactSha256"), source.get("sourceArtifactSha256")]
        if not any(value is not None for value in hashes):
            provenance = source.get("provenance")
            hashes = [provenance.get("targetArtifactSha256")] if isinstance(provenance, dict) else []
    present = [value for value in hashes if value is not None]
    if not present:
        return "unbound"
    return "same_source" if all(value == source_hash for value in present) else "source_mismatch"


def _documented_series(data: dict[str, Any] | None, duration: float,
                       *, percussive: bool = False) -> tuple[np.ndarray, np.ndarray, float] | None:
    """Use displayed measurements only when their averaging/window span is known."""
    if not isinstance(data, dict) or not isinstance(data.get("frameSeries"), dict):
        return None
    supplied = data["frameSeries"]
    step, hop, window = (_number(supplied.get(key)) for key in
                         ("intervalSeconds", "analysisHopSeconds", "analysisWindowSeconds"))
    if (step is None or hop is None or window is None or not 0 < hop <= step <= 0.5
            or not 0 < window <= 0.5 or not isinstance(supplied.get("aggregation"), str)):
        return None
    span = step - hop + window
    if span > 0.5:
        return None
    adapted = dict(data)
    adapted["windowSeconds"] = step
    if percussive:
        columns, points = supplied.get("columns"), supplied.get("points")
        if (not isinstance(columns, list) or not isinstance(points, (list, np.ndarray))
                or not 12 <= len(points) <= MAX_POINTS or "timeSeconds" not in columns
                or "percussiveRms" not in columns):
            return None
        ti, ri = columns.index("timeSeconds"), columns.index("percussiveRms")
        rows = []
        for row in points:
            if not isinstance(row, (list, tuple, np.ndarray)) or len(row) != len(columns):
                return None
            time, energy = _number(row[ti]), _number(row[ri])
            if time is None or energy is None or not 0 <= energy <= 10:
                return None
            rows.append([time, 20 * math.log10(energy) if energy > 0 else None])
        adapted["frameSeries"] = {"columns": ["timeSeconds", "rmsDbfs"], "points": rows}
    result = _series(adapted, duration)
    if result is None:
        return None
    times, rms, _, _ = result
    # An averaged point spans all constituent analysis windows. Checking only
    # its center/nominal display interval could overlap a changed PCM window.
    valid = np.zeros(len(times), dtype=bool)
    starts, ends = np.maximum(0, times - span / 2), np.minimum(duration, times + span / 2)
    for start, end in _ranges(data, duration):
        valid |= (starts >= start - 1e-6) & (ends <= end + 1e-6)
    rms = rms.copy()
    rms[~valid] = np.nan
    return times, rms, span


def _energy_context(series: tuple[np.ndarray, np.ndarray, float] | None,
                    start: float, end: float) -> dict[str, Any]:
    if series is None:
        return {"status": "unavailable"}
    times, rms, span = series
    values = []
    counts = []
    for first, last in ((start - 1, start), (start, end), (end, end + 1)):
        selected = (times - span / 2 >= first - 1e-6) & (times + span / 2 <= last + 1e-6)
        local = rms[selected]
        counts.append(int(len(local)))
        # A surviving subset of a partially invalid interval cannot stand in
        # for the complete context. No interpolation across gaps is allowed.
        if len(local) < (2 if first == start else 3) or not np.all(np.isfinite(local)):
            return {"status": "insufficient_valid_windows", "windowCounts": counts}
        values.append(float(np.median(local)))
    before, during, after = values
    return {"status": "observed", "beforeRmsDbfs": _rounded(before),
            "duringRmsDbfs": _rounded(during), "afterRmsDbfs": _rounded(after),
            "relativeFallDb": _rounded(min(before, after) - during), "windowCounts": counts}


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    left, right = left - np.mean(left), right - np.mean(right)
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    # Two flat flanks carry no shape evidence beyond the central dip.
    return float(left @ right / denominator) if denominator > 1e-8 else 0.0


def _repeated_envelope(times: np.ndarray, rms: np.ndarray, first: int, last: int,
                       step: float) -> dict[str, Any]:
    flank, length = max(2, math.ceil(REPEAT_FLANK_SECONDS / step)), last - first + 1
    thresholds = {"minimumOtherMatches": MIN_REPEAT_MATCHES, "flankSeconds": REPEAT_FLANK_SECONDS,
        "minimumCorrelation": MIN_REPEAT_CORRELATION, "minimumFlankCorrelation": MIN_FLANK_CORRELATION,
        "maximumMeanShapeErrorDb": MAX_REPEAT_ERROR_DB,
        "maximumDepthDifferenceDb": MAX_REPEAT_DEPTH_DIFFERENCE_DB,
        "maximumCenterLevelDifferenceDb": MAX_REPEAT_CENTER_DIFFERENCE_DB,
        "minimumPeerDipDepthDb": 14, "requiresNonoverlappingComparisonWindows": True}
    empty = {"status": "insufficient_context", "otherMatchCount": 0, "examples": [], "threshold": thresholds}
    if first < flank or last + flank >= len(times):
        return empty
    prototype = rms[first - flank:last + flank + 1]
    if not np.all(np.isfinite(prototype)):
        return empty
    proto_flanks = np.r_[prototype[:flank], prototype[-flank:]]
    proto_center = float(np.median(prototype[flank:flank + length]))
    proto_depth = float(np.median(proto_flanks)) - proto_center
    profile = prototype - float(np.median(proto_flanks))
    matches = []
    # At most 6,100 tested positions per bounded source, even when input frames
    # resolve 10 ms. All original frames in a comparison still must be valid.
    stride = max(1, math.ceil(0.1 / step))
    for index in range(flank, len(rms) - length - flank + 1, stride):
        if abs(index - first) < length + 2 * flank:
            continue
        block = rms[index - flank:index + length + flank]
        if not np.all(np.isfinite(block)):
            continue
        block_flanks = np.r_[block[:flank], block[-flank:]]
        center = float(np.median(block[flank:flank + length]))
        depth = float(np.median(block_flanks)) - center
        if (depth < 14 or abs(depth - proto_depth) > MAX_REPEAT_DEPTH_DIFFERENCE_DB
                or abs(center - proto_center) > MAX_REPEAT_CENTER_DIFFERENCE_DB):
            continue
        normalized = block - float(np.median(block_flanks))
        error = float(np.mean(np.abs(profile - normalized)))
        flank_error = float(np.mean(np.abs(np.r_[profile[:flank], profile[-flank:]]
                                              - np.r_[normalized[:flank], normalized[-flank:]])))
        correlation = _correlation(profile, normalized)
        flank_correlation = _correlation(proto_flanks, block_flanks)
        if (correlation >= MIN_REPEAT_CORRELATION and flank_correlation >= MIN_FLANK_CORRELATION
                and max(error, flank_error) <= MAX_REPEAT_ERROR_DB):
            matches.append((error, index, correlation, flank_correlation, center, depth))
    selected = []
    for match in sorted(matches):
        if all(abs(match[1] - previous[1]) >= length + 2 * flank for previous in selected):
            selected.append(match)
    examples = [{"startSeconds": _rounded(times[index] - step / 2),
                 "endSeconds": _rounded(times[index + length - 1] + step / 2),
                 "comparisonStartSeconds": _rounded(times[index - flank] - step / 2),
                 "comparisonEndSeconds": _rounded(times[index + length + flank - 1] + step / 2),
                 "correlation": _rounded(correlation), "flankCorrelation": _rounded(flank_correlation),
                 "meanShapeErrorDb": _rounded(error), "duringRmsDbfs": _rounded(center),
                 "relativeDepthDb": _rounded(depth)}
                for error, index, correlation, flank_correlation, center, depth in
                sorted(selected, key=lambda item: item[1])[:MAX_REPEAT_EXAMPLES]]
    return {"status": "recurring_shape_observed" if len(selected) >= MIN_REPEAT_MATCHES else "not_established",
            "otherMatchCount": len(selected), "examples": examples, "threshold": thresholds,
            "interpretation": "Similar measured dips and surrounding envelopes recur; this is compatible with phrasing or repeated corruption and does not determine intention."}


def add_rhythm_context(diagnosis: dict[str, Any], *, duration_seconds: float, source_sha256: str,
                       accompaniment: dict[str, Any] | None = None, vocal: dict[str, Any] | None = None,
                       percussion: dict[str, Any] | None = None,
                       structure: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy a diagnosis and attach source-validated context to backing events.

    Severity, retry eligibility, intent and detector outcome are preserved.
    Review priority summarizes which alternate explanations or cross-cues are
    measurable, without treating any of them as a human listening decision.
    """
    duration = _number(duration_seconds)
    if duration is None or not 0 < duration <= MAX_SECONDS:
        raise ValueError("Rhythm context duration must be finite and bounded")
    if not isinstance(diagnosis, dict):
        raise ValueError("Rhythm context requires a diagnosis object")
    result = deepcopy(diagnosis)
    supplied = {"accompaniment": accompaniment, "vocal": vocal, "percussion": percussion, "structure": structure}
    bindings = {key: _binding(data, source_sha256, structure=key == "structure") for key, data in supplied.items()}
    backing = _series(accompaniment, duration) if bindings["accompaniment"] == "same_source" else None
    vocals = _series(vocal, duration) if bindings["vocal"] == "same_source" else None
    mix = _documented_series(structure, duration) if bindings["structure"] == "same_source" else None
    transient = _documented_series(percussion, duration, percussive=True) if bindings["percussion"] == "same_source" else None
    priorities = {key: 0 for key in ("source_conflict", "isolated_anomaly", "recurring_pattern", "insufficient_context")}
    examined = 0
    events = result.get("events")
    if not isinstance(events, list):
        events = []
    for event in events:
        if not isinstance(event, dict) or event.get("category") not in {"backing_dropout", "arrangement_break"}:
            continue
        examined += 1
        priority = "insufficient_context"
        context: dict[str, Any] = {"sourceBindings": dict(bindings), "intent": "unknown"}
        start, end = _number(event.get("startSeconds")), _number(event.get("endSeconds"))
        if backing is not None and start is not None and end is not None and 0 <= start < end <= duration:
            times, rms, _, step = backing
            inside = np.flatnonzero((times - step / 2 >= start - 1e-6) & (times + step / 2 <= end + 1e-6))
            flank = max(3, math.ceil(1 / step))
            if (len(inside) and inside[0] >= flank and inside[-1] + flank < len(times)
                    and np.all(np.isfinite(rms[inside[0] - flank:inside[-1] + flank + 1]))):
                first, last = int(inside[0]), int(inside[-1])
                before = float(np.median(rms[first - flank:first]))
                middle = float(np.median(rms[first:last + 1]))
                after = float(np.median(rms[last + 1:last + flank + 1]))
                backing_fall = min(before, after) - middle
                repeated = _repeated_envelope(times, rms, first, last, step)
                mix_context, transient_context = _energy_context(mix, start, end), _energy_context(transient, start, end)
                context.update({"repeatedEnvelope": repeated, "originalMix": mix_context,
                                "spectralTransientProxy": transient_context, "backingRelativeFallDb": _rounded(backing_fall)})
                vocal_to_mix = None
                if vocals is not None and mix_context.get("status") == "observed":
                    vt, vr, _, vs = vocals
                    active = (vt - vs / 2 >= start - 1e-6) & (vt + vs / 2 <= end + 1e-6)
                    if np.count_nonzero(active) and np.all(np.isfinite(vr[active])):
                        vocal_to_mix = float(np.median(vr[active])) - mix_context["duringRmsDbfs"]
                context["estimatedVocalToMixDb"] = _rounded(vocal_to_mix)
                cue = "unresolved"
                if mix_context.get("status") == transient_context.get("status") == "observed":
                    mix_fall, transient_fall = mix_context["relativeFallDb"], transient_context["relativeFallDb"]
                    if (backing_fall >= 14 and mix_fall <= 6 and transient_fall <= 3
                            and transient_context["duringRmsDbfs"] > -55
                            and vocal_to_mix is not None and abs(vocal_to_mix) <= 1.5):
                        cue, priority = "source_allocation_conflict", "source_conflict"
                    elif mix_fall >= 10 and transient_fall >= 10:
                        cue, priority = "mix_and_transient_attenuation_observed", "isolated_anomaly"
                if priority != "source_conflict" and repeated["status"] == "recurring_shape_observed":
                    priority = "recurring_pattern"
                context["crossCueStatus"] = cue
                context["crossCueThreshold"] = {"maximumContinuousMixFallDb": 6, "maximumContinuousTransientFallDb": 3,
                    "maximumEstimatedVocalToMixDifferenceDb": 1.5, "minimumCorroboratingFallDb": 10,
                    "minimumTransientRmsDbfs": -55, "requiresSameSourceAndCompleteWindows": True}
                context["interpretation"] = "Cross-cues can corroborate attenuation or expose disagreement between a residual backing estimate and observed mix transients; they do not establish a defect or musical intention."
        observed = event.get("observed")
        if not isinstance(observed, dict):
            observed = event["observed"] = {}
        observed["contextEvidence"] = context
        event["reviewPriority"] = priority
        priorities[priority] += 1
    # Findings is commonly the same event list serialized under another key.
    # Keep its new contextual fields aligned without rewriting detector facts.
    findings = result.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            for event in events:
                if (isinstance(event, dict) and event.get("reviewPriority") is not None
                        and all(finding.get(key) == event.get(key) for key in
                                ("check", "category", "startSeconds", "endSeconds"))):
                    finding["reviewPriority"] = event["reviewPriority"]
                    finding.setdefault("observed", {})["contextEvidence"] = deepcopy(event["observed"]["contextEvidence"])
                    break
    result["contextVersion"] = VERSION
    result["contextSummary"] = {"backingEventsExamined": examined, "reviewPriorityCounts": priorities,
        "sourceBindings": bindings, "severityChanged": False, "intentDetermined": False}
    return result
