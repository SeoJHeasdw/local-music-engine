"""Source-attributed diagnosis separates timing, missing pulses and backing loss."""

from copy import deepcopy
import json

import numpy as np
import pytest

from local_music_engine.rhythm_diagnostics import LEGACY_VERSION, MAX_EVENTS, VERSION, diagnose_rhythm


def percussion(times=None, *, bpm=120, onsets=None, segments=None, confidence=0.9):
    times = np.arange(0.0, 40.0, 0.5) if times is None else np.asarray(times)
    return {"source": {"kind": "synthetic_percussion", "method": "known PCM impulse locations", "reliability": 1.0},
            "pulse": {"bpm": bpm, "confidence": confidence},
            "onsetTimesSeconds": times.tolist() if onsets is None else np.asarray(onsets).tolist(),
            "beatCandidatesSeconds": times.tolist(), "segments": segments or []}


def stems(*, duration=40.0, start=16.0, end=18.0, step=0.1, depth=40.0,
          vocal_drop=False, mix_drop=False):
    times = np.arange(step / 2, duration, step)
    backing = np.full(len(times), -18.0)
    vocal = np.full(len(times), -22.0)
    mix = np.full(len(times), -16.0)
    region = (times >= start) & (times < end)
    backing[region] -= depth
    vocal[region] -= depth if vocal_drop else 0
    mix[region] -= depth if mix_drop else 5
    return ({"source": {"kind": "synthetic_accompaniment", "method": "separate generated backing", "reliability": 1.0},
             "frameSeries": {"columns": ["timeSeconds", "rmsDbfs", "mixRmsDbfs"],
                             "points": np.column_stack((times, backing, mix)).tolist()}},
            {"source": {"kind": "synthetic_vocal", "method": "separate generated voice", "reliability": 1.0},
             "frameSeries": {"columns": ["timeSeconds", "rmsDbfs"],
                             "points": np.column_stack((times, vocal)).tolist()}})


def only(report, category):
    return [event for event in report["events"] if event["category"] == category]


def test_steady_percussion_and_vocal_masked_backing_drop_are_different_observations():
    backing, vocal = stems(start=16.0, end=18.5)
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(), accompaniment=backing, vocal=vocal)
    assert report["version"] == VERSION
    assert report["status"] == "needs_review"
    assert report["checks"]["beatTiming"]["status"] == "observed"
    assert report["checks"]["backingContinuity"]["status"] == "needs_review"
    assert not only(report, "beat_timing")
    event, = only(report, "backing_dropout")
    assert event["startSeconds"] == pytest.approx(16.0)
    assert event["endSeconds"] == pytest.approx(18.5)
    assert event["observed"]["relativeDepthDb"] == 40
    assert event["observed"]["vocalActiveFraction"] == 1
    assert event["observed"]["mixContinuityDb"] == -5
    assert event["observed"]["intent"] == "unknown"
    assert event["evidenceStatus"] == "intent_unknown"
    assert event["retryEligible"] is False
    assert report["findings"] == report["events"]
    json.dumps(report, allow_nan=False)


def test_local_jitter_with_stable_flanks_does_not_claim_backing_dropout():
    beats = np.arange(0.0, 40.0, 0.5)
    beats[32:40] += np.array([0, 0.08, -0.05, 0.065, -0.07, 0.09, -0.08, 0])
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(beats))
    event, = only(report, "beat_timing")
    assert report["status"] == "needs_review"
    assert report["checks"]["backingContinuity"]["status"] == "unknown"
    assert event["observed"]["mode"] == "jitter"
    assert event["observed"]["timingDeviationP95Milliseconds"] > 100
    assert event["observed"]["missingPulseCount"] == 0
    assert event["observed"]["onsetDensityRatioBefore"] < 1.5
    assert not only(report, "backing_dropout")


def test_independent_segment_tempo_drift_must_agree_with_actual_candidates():
    beats = np.r_[np.arange(0, 12, 0.5), np.arange(12, 42, 0.6)]
    segments = [{"startSeconds": 0, "endSeconds": 12, "bpm": 120, "confidence": 0.9},
                {"startSeconds": 12, "endSeconds": 24, "bpm": 100, "confidence": 0.9},
                {"startSeconds": 24, "endSeconds": 42, "bpm": 100, "confidence": 0.9}]
    report = diagnose_rhythm(duration_seconds=42, percussion=percussion(beats, segments=segments))
    event, = only(report, "beat_timing")
    assert event["observed"]["mode"] == "tempo_drift"
    assert event["observed"]["afterBpm"] == 100
    assert event["observed"]["periodChangePercent"] == 20
    # An independent estimator changing its mind without changed observed
    # intervals is not corroborated tempo-drift evidence.
    unsupported = diagnose_rhythm(duration_seconds=40, percussion=percussion(segments=segments[:2]))
    assert not only(unsupported, "beat_timing")


def test_missing_percussion_is_not_timing_jitter_or_backing_loss():
    beats = np.delete(np.arange(0, 40, 0.5), np.arange(32, 37))
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(beats))
    assert not only(report, "beat_timing")
    assert not only(report, "backing_dropout")
    gap, = only(report, "percussion_gap")
    assert gap["observed"]["missingPulseCount"] == 5
    assert gap["severity"] == "info"
    assert gap["evidenceStatus"] == "observed"
    assert report["status"] == "unknown"  # Missing independent backing stems.


def test_phase_filter_cannot_hide_a_supported_late_observed_percussion_onset():
    onsets = np.arange(0.0, 40.0, 0.5)
    onsets[32] += 0.13
    candidates = np.delete(onsets, 32)  # A narrow +/-90 ms phase filter.
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(candidates, onsets=onsets))
    event, = only(report, "beat_timing")
    assert event["observed"]["mode"] == "jitter"
    assert event["observed"]["timingDeviationP95Milliseconds"] == 130
    assert report["checks"]["beatTiming"]["observed"]["recoveredObservedOnsetCount"] == 1
    assert not only(report, "percussion_gap")


def test_contested_peaks_near_a_missing_candidate_remain_unknown():
    observed = np.arange(0.0, 40.0, 0.5)
    candidates = np.delete(observed, 32)
    onsets = np.sort(np.r_[candidates, 16.08, 16.13])
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(candidates, onsets=onsets))
    assert report["checks"]["beatTiming"]["status"] == "unknown"
    assert report["checks"]["beatTiming"]["observed"]["ambiguousPulseGapCount"] == 1
    assert report["checks"]["beatTiming"]["observed"]["countRecoveredOnsets"] == 0
    assert not report["events"]


def test_onset_density_change_excludes_fill_from_jitter_claim():
    beats = np.arange(0.0, 40.0, 0.5)
    beats[32:40] += np.array([0, 0.08, -0.05, 0.065, -0.07, 0.09, -0.08, 0])
    onsets = np.sort(np.r_[beats, np.arange(16.1, 19.6, 0.12)])
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(beats, onsets=onsets))
    assert not only(report, "beat_timing")
    assert report["checks"]["beatTiming"]["observed"]["excludedOnsetDensityChanges"] >= 1


@pytest.mark.parametrize("bpm,period", [(60, 1.0), (240, 0.25), (180, 1 / 3)])
def test_half_double_and_compound_alias_segments_are_not_tempo_defects(bpm, period):
    beats = np.r_[np.arange(0, 12, 0.5), np.arange(12, 36, period)]
    segments = [{"startSeconds": 0, "endSeconds": 12, "bpm": 120, "confidence": 0.9},
                {"startSeconds": 12, "endSeconds": 36, "bpm": bpm, "confidence": 0.9}]
    report = diagnose_rhythm(duration_seconds=36, percussion=percussion(beats, segments=segments))
    assert not only(report, "beat_timing")


def test_artificial_grid_and_harmonic_onsets_cannot_support_drum_claims():
    harmonic = percussion()
    harmonic["source"]["kind"] = "hpss_harmonic"
    assert diagnose_rhythm(duration_seconds=40, percussion=harmonic)["checks"]["beatTiming"]["status"] == "unknown"
    grid = percussion()
    grid["onsetTimesSeconds"] = (np.arange(0, 40, 0.5) + 0.08).tolist()
    grid["onsetTimesSeconds"].pop()
    grid["beatCandidatesSeconds"].pop()
    report = diagnose_rhythm(duration_seconds=40, percussion=grid)
    assert report["checks"]["beatTiming"]["status"] == "unknown"
    assert not report["events"]


def test_hpss_harmonic_proxy_cannot_classify_accompaniment_dropout():
    backing, vocal = stems()
    backing["source"]["kind"] = "hpss_harmonic"
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(), accompaniment=backing, vocal=vocal)
    assert report["checks"]["beatTiming"]["status"] == "observed"
    assert report["checks"]["backingContinuity"]["status"] == "unknown"
    assert report["status"] == "unknown"
    assert not report["events"]


@pytest.mark.parametrize("kwargs", [{"vocal_drop": True}, {"mix_drop": True}])
def test_global_silence_or_contradictory_mix_fall_is_not_backing_only_dropout(kwargs):
    backing, vocal = stems(**kwargs)
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    assert not only(report, "backing_dropout")


def test_loud_backing_drop_can_lower_mix_while_quiet_vocal_remains_active():
    backing, vocal = stems()
    backing_rows = np.asarray(backing["frameSeries"]["points"])
    vocal_rows = np.asarray(vocal["frameSeries"]["points"])
    region = (backing_rows[:, 0] >= 16) & (backing_rows[:, 0] < 18)
    backing_rows[:, 1] = np.where(region, -65, -6)
    backing_rows[:, 2] = np.where(region, -38, -6)
    vocal_rows[:, 1] = -38
    backing["frameSeries"]["points"] = backing_rows.tolist()
    vocal["frameSeries"]["points"] = vocal_rows.tolist()
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    event, = only(report, "backing_dropout")
    assert event["observed"]["mixContinuityDb"] == -32
    assert event["observed"]["vocalContinuityDb"] == 0


def test_audible_vocal_inside_drop_has_evidence_even_when_both_flanks_are_breaths():
    backing, vocal = stems()
    rows = np.asarray(vocal["frameSeries"]["points"])
    rows[:, 1] = -80
    rows[(rows[:, 0] >= 16) & (rows[:, 0] < 18), 1] = -25
    vocal["frameSeries"]["points"] = rows.tolist()
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    event, = only(report, "backing_dropout")
    assert event["observed"]["vocalActiveFraction"] == 1
    assert event["observed"]["vocalBeforeRmsDbfs"] == -80


@pytest.mark.parametrize("start,end", [(0.0, 3.0), (37.0, 40.0)])
def test_intro_and_outro_do_not_have_two_interior_flanks(start, end):
    backing, vocal = stems(start=start, end=end)
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    assert not report["events"]


def test_smooth_local_fade_and_no_rebound_are_not_abrupt_dropouts():
    backing, vocal = stems(depth=0)
    rows = np.asarray(backing["frameSeries"]["points"])
    times = rows[:, 0]
    triangle = np.maximum(0, 1 - np.abs(times - 18) / 4)
    rows[:, 1] -= triangle * 40
    backing["frameSeries"]["points"] = rows.tolist()
    assert not diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)["events"]
    rows[times >= 16, 1] = -65
    backing["frameSeries"]["points"] = rows.tolist()
    assert not diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)["events"]


def test_expected_rest_requires_explicit_metadata_not_grid_or_section_boundary():
    backing, vocal = stems(start=16, end=18)
    regular_section = [{"startSeconds": 16, "endSeconds": 18, "name": "bridge"}]
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(), accompaniment=backing, vocal=vocal,
                             expected_sections=regular_section)
    assert only(report, "backing_dropout")
    rest = [{"startSeconds": 16, "endSeconds": 18, "expectedRest": True}]
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(), accompaniment=backing, vocal=vocal,
                             expected_sections=rest)
    event, = only(report, "arrangement_break")
    assert report["status"] == "observed"
    assert event["check"] == "possible_arrangement_break"
    assert event["severity"] == "info"
    assert event["observed"]["intent"] == "unknown"


def test_source_validated_range_excludes_a_drop_in_unmatched_revision_audio():
    backing, vocal = stems(start=16, end=18)
    for source in (backing, vocal):
        source["validRangeSeconds"] = [20, 40]
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    assert not report["events"]
    backing, vocal = stems(start=26, end=28)
    for source in (backing, vocal):
        source["validatedRanges"] = [{"startSeconds": 20, "endSeconds": 40}]
    assert only(diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal), "backing_dropout")


def test_percussion_validity_gap_does_not_become_a_claimed_percussion_silence():
    source = percussion()
    source["validatedRanges"] = [[0, 14], [22, 40]]
    report = diagnose_rhythm(duration_seconds=40, percussion=source)
    assert report["checks"]["beatTiming"]["status"] == "observed"
    assert report["checks"]["beatTiming"]["observed"]["missingPulseCount"] == 0
    assert not report["events"]


def test_cropped_existing_stem_windows_use_absolute_track_time():
    backing, vocal = stems(duration=180, start=42.8, end=45.2, step=0.25)
    for source in (backing, vocal):
        source["validRangeSeconds"] = [30, 180]
        source["frameSeries"]["points"] = [row for row in source["frameSeries"]["points"] if row[0] >= 30]
    report = diagnose_rhythm(duration_seconds=180, accompaniment=backing, vocal=vocal)
    event, = only(report, "backing_dropout")
    assert event["startSeconds"] == pytest.approx(42.75)
    assert event["endSeconds"] == pytest.approx(45.25)


def test_frozen_legacy_rubato_intent_makes_timing_observation_informational():
    beats = np.arange(0.0, 40.0, 0.5)
    beats[32:40] += np.array([0, 0.08, -0.05, 0.065, -0.07, 0.09, -0.08, 0])
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(beats), style_prompt="expressive rubato piano",
                             diagnostics_version=LEGACY_VERSION)
    event, = only(report, "beat_timing")
    assert event["severity"] == "info"
    assert report["status"] == "unknown"
    assert event["retryEligible"] is False


def test_missing_and_low_reliability_evidence_remains_unknown_and_json_safe():
    report = diagnose_rhythm(duration_seconds=40)
    assert report["status"] == "unknown"
    assert report["checks"]["beatTiming"]["status"] == "unknown"
    assert report["checks"]["backingContinuity"]["status"] == "unknown"
    assert not report["events"]
    weak = percussion(confidence=0.3)
    assert diagnose_rhythm(duration_seconds=40, percussion=weak)["status"] == "unknown"
    unreliable = percussion()
    unreliable["source"]["reliability"] = 0.2
    assert not diagnose_rhythm(duration_seconds=40, percussion=unreliable)["events"]
    assert len(report["events"]) <= MAX_EVENTS
    json.dumps(report, allow_nan=False)


def test_strong_adjacent_local_pulses_can_diagnose_jitter_under_weak_global_pulse():
    beats = np.arange(0.0, 80.0, 0.5)
    beats[100:108] += np.array([0, 0.08, -0.05, 0.065, -0.07, 0.09, -0.08, 0])
    segments = [{"startSeconds": 40, "endSeconds": 52, "bpm": 120, "confidence": 0.8},
                {"startSeconds": 52, "endSeconds": 64, "bpm": 120, "confidence": 0.85}]
    report = diagnose_rhythm(duration_seconds=80, percussion=percussion(beats, segments=segments, confidence=0.4))
    event, = only(report, "beat_timing")
    assert report["checks"]["beatTiming"]["status"] == "needs_review"
    assert event["startSeconds"] >= 40 and event["endSeconds"] <= 64
    ranges = report["checks"]["beatTiming"]["observed"]["localSupportedRanges"]
    assert ranges[0]["startSeconds"] == 40
    assert ranges[0]["endSeconds"] == 64


def test_one_confident_segment_cannot_override_unknown_whole_track_pulse():
    segments = [{"startSeconds": 12, "endSeconds": 24, "bpm": 120, "confidence": 0.9}]
    report = diagnose_rhythm(duration_seconds=40, percussion=percussion(segments=segments, confidence=0.4))
    assert report["checks"]["beatTiming"]["status"] == "unknown"
    assert not report["events"]


@pytest.mark.parametrize("duration", [0, -1, 611, float("inf"), float("nan"), True])
def test_invalid_duration_is_rejected(duration):
    with pytest.raises(ValueError):
        diagnose_rhythm(duration_seconds=duration)


@pytest.mark.parametrize("mutation", ["nan", "unordered", "invalid_range", "coarse"])
def test_malformed_stem_features_cannot_become_successful_diagnosis(mutation):
    backing, vocal = stems()
    original = deepcopy(backing)
    if mutation == "nan":
        backing["frameSeries"]["points"][40][1] = float("nan")
    elif mutation == "unordered":
        backing["frameSeries"]["points"][40][0] = 0
    elif mutation == "invalid_range":
        backing["validRangeSeconds"] = [30, 20]
    else:
        backing["frameSeries"]["points"] = backing["frameSeries"]["points"][::10]
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    assert report["checks"]["backingContinuity"]["status"] == "unknown"
    assert not report["events"]
    json.dumps(report, allow_nan=False)
    assert original != backing
