"""Context compares measured source windows without making listening decisions."""

from copy import deepcopy
import json

import numpy as np
import pytest

from local_music_engine.rhythm_context import VERSION, add_rhythm_context

SOURCE = "a" * 64
OTHER_SOURCE = "b" * 64


def diagnosis(start=18.0, end=18.25):
    event = {"category": "backing_dropout", "check": "backing_dropout_suspected", "severity": "warning",
        "startSeconds": start, "endSeconds": end, "retryEligible": False, "evidenceStatus": "intent_unknown",
        "observed": {"intent": "unknown", "relativeDepthDb": 24}}
    return {"status": "needs_review", "events": [event], "findings": [deepcopy(event)],
            "checks": {"backingContinuity": {"status": "needs_review"}}}


def measured_inputs(backing_db, *, vocal_db=None, uniform_corruption=None):
    """Measure known PCM components and full mix on actual 125/250 ms windows."""
    backing_db = np.asarray(backing_db, dtype=float)
    vocal_db = np.full(len(backing_db), -24.0) if vocal_db is None else np.asarray(vocal_db, dtype=float)
    rate, window, samples_per_window = 8000, 0.25, 2000
    time = np.arange(len(backing_db) * samples_per_window) / rate
    backing = np.repeat(10 ** (backing_db / 20), samples_per_window) * np.sqrt(2) * np.sin(2 * np.pi * 120 * time)
    vocal = np.repeat(10 ** (vocal_db / 20), samples_per_window) * np.sqrt(2) * np.sin(2 * np.pi * 440 * time)
    if uniform_corruption:
        start, end, gain = uniform_corruption
        selected = (time >= start) & (time < end)
        backing[selected] *= gain
        vocal[selected] *= gain
    mix = backing + vocal

    def rms(samples, length):
        return np.sqrt(np.mean(samples.reshape(-1, length) ** 2, axis=1))

    def db(values):
        return 20 * np.log10(np.maximum(values, 1e-12))

    stem_times = (np.arange(len(backing_db)) + 0.5) * window
    common = {"windowSeconds": window, "source": {"sourceArtifactSha256": SOURCE, "reliability": 1.0}}
    accompaniment = {**common, "source": {**common["source"], "kind": "synthetic_accompaniment"},
        "frameSeries": {"columns": ["timeSeconds", "rmsDbfs", "mixRmsDbfs"],
                        "points": np.c_[stem_times, db(rms(backing, samples_per_window)), db(rms(mix, samples_per_window))].tolist()}}
    vocals = {**common, "source": {**common["source"], "kind": "synthetic_vocal"},
        "frameSeries": {"columns": ["timeSeconds", "rmsDbfs"],
                        "points": np.c_[stem_times, db(rms(vocal, samples_per_window))].tolist()}}
    detail_step = 0.125
    detailed_times = (np.arange(len(backing_db) * 2) + 0.5) * detail_step
    documented = {"intervalSeconds": detail_step, "analysisHopSeconds": detail_step,
                  "analysisWindowSeconds": detail_step, "aggregation": "known PCM window RMS"}
    structure = {"measuredArtifactSha256": SOURCE, "frameSeries": {**documented,
        "columns": ["timeSeconds", "rmsDbfs"],
        "points": np.c_[detailed_times, db(rms(mix, samples_per_window // 2))].tolist()}}
    percussion = {"sourceArtifactSha256": SOURCE, "source": {"kind": "synthetic_percussion", "reliability": 1.0},
        "frameSeries": {**documented, "columns": ["timeSeconds", "percussiveRms"],
        "points": np.c_[detailed_times, rms(backing, samples_per_window // 2)].tolist()}}
    return {"accompaniment": accompaniment, "vocal": vocals, "structure": structure, "percussion": percussion}


def contextualize(report, inputs, duration=64):
    return add_rhythm_context(report, duration_seconds=duration, source_sha256=SOURCE, **inputs)


def periodic_envelope():
    # The flanks also vary and repeat; a central low frame alone is insufficient.
    return np.tile([-18, -17, -20, -22, -18, -19, -22, -24, -42, -24, -20, -18, -16, -18, -21, -20], 16)


def test_recurring_pcm_envelope_adds_peer_locations_but_preserves_warning_and_unknown_intent():
    report = diagnosis()
    before = deepcopy(report)
    result = contextualize(report, measured_inputs(periodic_envelope()))
    event, = result["events"]
    assert event["reviewPriority"] == "recurring_pattern"
    repetition = event["observed"]["contextEvidence"]["repeatedEnvelope"]
    assert repetition["status"] == "recurring_shape_observed"
    assert repetition["otherMatchCount"] >= 3
    assert len(repetition["examples"]) <= 8
    ranges = [(item["comparisonStartSeconds"], item["comparisonEndSeconds"]) for item in repetition["examples"]]
    assert all(left[1] <= right[0] for left, right in zip(ranges, ranges[1:]))
    assert all(end <= 16 or start >= 20.25 for start, end in ranges)
    assert event["severity"] == "warning"
    assert event["retryEligible"] is False
    assert event["observed"]["intent"] == "unknown"
    assert result["status"] == "needs_review"
    assert result["findings"] == result["events"]
    assert result["contextVersion"] == VERSION
    assert result["contextSummary"]["severityChanged"] is False
    assert report == before
    json.dumps(result, allow_nan=False)


def test_flat_flanks_with_identical_repeated_corruption_are_not_shape_evidence():
    envelope = np.full(256, -18.0)
    envelope[8::16] = -42
    result = contextualize(diagnosis(), measured_inputs(envelope))
    event, = result["events"]
    repeated = event["observed"]["contextEvidence"]["repeatedEnvelope"]
    assert repeated["otherMatchCount"] == 0
    assert repeated["status"] == "not_established"
    assert event["severity"] == "warning"


def test_real_pcm_uniform_gain_corruption_is_corroborated_without_claiming_intent():
    inputs = measured_inputs(np.full(256, -18.0), uniform_corruption=(24, 25, 0.01))
    result = contextualize(diagnosis(24, 25), inputs)
    event, = result["events"]
    context = event["observed"]["contextEvidence"]
    assert context["originalMix"]["relativeFallDb"] == pytest.approx(40)
    assert context["spectralTransientProxy"]["relativeFallDb"] == pytest.approx(40)
    assert context["crossCueStatus"] == "mix_and_transient_attenuation_observed"
    assert event["reviewPriority"] == "isolated_anomaly"
    assert event["severity"] == "warning"
    assert context["intent"] == "unknown"


def test_residual_allocation_error_conflicts_with_measured_continuous_mix_transients():
    inputs = measured_inputs(np.full(256, -18.0))
    # Simulate the separator assigning nearly the whole observed mixture to
    # its vocal estimate, without removing any PCM component from the source.
    for backing_row, vocal_row in zip(inputs["accompaniment"]["frameSeries"]["points"],
                                       inputs["vocal"]["frameSeries"]["points"]):
        if 24 <= backing_row[0] < 25:
            backing_row[1] = -42
            vocal_row[1] = backing_row[2] - 0.2
    result = contextualize(diagnosis(24, 25), inputs)
    event, = result["events"]
    context = event["observed"]["contextEvidence"]
    assert context["crossCueStatus"] == "source_allocation_conflict"
    assert context["originalMix"]["relativeFallDb"] == pytest.approx(0)
    assert context["spectralTransientProxy"]["relativeFallDb"] == pytest.approx(0)
    assert context["estimatedVocalToMixDb"] == pytest.approx(-0.2)
    assert event["reviewPriority"] == "source_conflict"
    assert event["severity"] == "warning"
    assert event["retryEligible"] is False


@pytest.mark.parametrize("source_key, hash_key", [("structure", "measuredArtifactSha256"),
                                                  ("percussion", "sourceArtifactSha256")])
def test_different_source_or_pre_finish_hash_cannot_supply_cross_corroboration(source_key, hash_key):
    inputs = measured_inputs(np.full(256, -18.0), uniform_corruption=(24, 25, 0.01))
    inputs[source_key][hash_key] = OTHER_SOURCE
    result = contextualize(diagnosis(24, 25), inputs)
    event, = result["events"]
    context = event["observed"]["contextEvidence"]
    assert context["sourceBindings"][source_key] == "source_mismatch"
    assert context["crossCueStatus"] == "unresolved"
    assert event["reviewPriority"] == "insufficient_context"
    assert event["severity"] == "warning"


def test_cached_pcm_validated_target_domain_is_used_but_original_native_hash_is_not_overridden():
    inputs = measured_inputs(periodic_envelope())
    for key in ("accompaniment", "vocal"):
        source = inputs[key]["source"]
        source.pop("sourceArtifactSha256")
        source["provenance"] = {"targetArtifactSha256": SOURCE, "sourceArtifactSha256": OTHER_SOURCE}
    assert contextualize(diagnosis(), inputs)["contextSummary"]["sourceBindings"]["accompaniment"] == "same_source"
    inputs["accompaniment"]["source"]["sourceArtifactSha256"] = OTHER_SOURCE
    result = contextualize(diagnosis(), inputs)
    assert result["contextSummary"]["sourceBindings"]["accompaniment"] == "source_mismatch"
    assert result["events"][0]["reviewPriority"] == "insufficient_context"


def test_support_gap_inside_event_prevents_context_and_does_not_turn_missing_values_into_silence():
    inputs = measured_inputs(periodic_envelope())
    inputs["accompaniment"]["validatedRanges"] = [[0, 18], [18.25, 64]]
    result = contextualize(diagnosis(), inputs)
    context = result["events"][0]["observed"]["contextEvidence"]
    assert "repeatedEnvelope" not in context
    assert result["events"][0]["reviewPriority"] == "insufficient_context"
    json.dumps(result, allow_nan=False)


def test_peer_comparisons_require_their_complete_surrounding_valid_windows():
    inputs = measured_inputs(periodic_envelope())
    inputs["accompaniment"]["validatedRanges"] = [[15.5, 20.5]]
    result = contextualize(diagnosis(), inputs)
    repetition = result["events"][0]["observed"]["contextEvidence"]["repeatedEnvelope"]
    assert repetition["otherMatchCount"] == 0
    assert repetition["examples"] == []


def test_similar_normalized_shape_at_different_absolute_center_energy_does_not_count_as_repeat():
    envelope = periodic_envelope()
    envelope += 12
    envelope[64:80] -= 12  # Only the target cycle has the actual measured level.
    inputs = measured_inputs(envelope)
    result = contextualize(diagnosis(), inputs)
    assert result["events"][0]["observed"]["contextEvidence"]["repeatedEnvelope"]["otherMatchCount"] == 0


@pytest.mark.parametrize("mutation", ["undocumented_windows", "missing_hash", "partial_window_gap"])
def test_transient_context_abstains_without_documented_full_same_source_windows(mutation):
    inputs = measured_inputs(np.full(256, -18.0), uniform_corruption=(24, 25, 0.01))
    percussion = inputs["percussion"]
    if mutation == "undocumented_windows":
        percussion["frameSeries"].pop("analysisWindowSeconds")
    elif mutation == "missing_hash":
        percussion.pop("sourceArtifactSha256")
    else:
        percussion["validatedRanges"] = [[0, 24.45], [24.55, 64]]
    result = contextualize(diagnosis(24, 25), inputs)
    context = result["events"][0]["observed"]["contextEvidence"]
    assert context["spectralTransientProxy"]["status"] != "observed"
    assert context["crossCueStatus"] == "unresolved"
    assert result["events"][0]["severity"] == "warning"
