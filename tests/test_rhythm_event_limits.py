"""Report display limits preserve late warnings and measured check outcomes."""

import numpy as np
import pytest

from local_music_engine.rhythm_diagnostics import MAX_EVENTS, diagnose_rhythm


def percussion_with_many_rests(*, jitter=False, confidence=0.9):
    beats = np.delete(np.arange(0.0, 400.0, 0.5), np.arange(20, 670, 10))
    if jitter:
        beats[(beats >= 380) & (beats < 384)] += np.array([0, 0.08, -0.05, 0.065, -0.07, 0.09, -0.08, 0])
    return {
        "source": {"kind": "synthetic_percussion", "method": "known fixture attacks", "reliability": 0.9},
        "pulse": {"bpm": 120, "confidence": confidence},
        "beatCandidatesSeconds": beats.tolist(),
        "onsetTimesSeconds": beats.tolist(),
        "segments": [{"startSeconds": start, "endSeconds": min(400, start + 12), "bpm": 120, "confidence": 0.9}
                     for start in range(0, 400, 12)],
    }


def separated_drops(duration, starts):
    times = np.arange(0.125, duration, 0.25)
    backing = np.full(len(times), -18.0)
    for start in starts:
        backing[(times >= start) & (times < start + 0.5)] = -50
    columns = ["timeSeconds", "rmsDbfs"]
    return (
        {"source": {"kind": "synthetic_accompaniment", "method": "known fixture backing", "reliability": 0.9},
         "frameSeries": {"columns": columns, "points": np.column_stack((times, backing)).tolist()}},
        {"source": {"kind": "synthetic_vocal", "method": "known fixture vocal", "reliability": 0.9},
         "frameSeries": {"columns": columns, "points": np.column_stack((times, np.full(len(times), -25))).tolist()}},
    )


def assert_chronological(events):
    starts = [event["startSeconds"] for event in events]
    assert starts == sorted(starts)


def test_late_backing_warning_survives_many_earlier_percussion_rest_observations():
    backing, vocal = separated_drops(400, [380])
    report = diagnose_rhythm(duration_seconds=400, percussion=percussion_with_many_rests(),
                             accompaniment=backing, vocal=vocal)
    assert report["checks"]["backingContinuity"]["status"] == "needs_review"
    assert report["status"] == "needs_review"
    assert len(report["events"]) == MAX_EVENTS
    warning, = [event for event in report["events"] if event["severity"] == "warning"]
    assert warning["category"] == "backing_dropout"
    assert warning["startSeconds"] == 380
    assert report["eventsTruncated"] is True
    assert report["totalEventCount"] == 66
    assert report["omittedEventCount"] == 2
    assert report["omittedWarningCount"] == 0
    assert_chronological(report["events"])


@pytest.mark.parametrize("confidence", [0.9, 0.4])
def test_late_timing_warning_survives_whole_or_local_timing_event_limits(confidence):
    report = diagnose_rhythm(duration_seconds=400,
                             percussion=percussion_with_many_rests(jitter=True, confidence=confidence))
    assert report["checks"]["beatTiming"]["status"] == "needs_review"
    assert report["status"] == "needs_review"
    assert len(report["events"]) == MAX_EVENTS
    warning, = [event for event in report["events"] if event["severity"] == "warning"]
    assert warning["category"] == "beat_timing"
    assert warning["startSeconds"] >= 380
    assert report["eventsTruncated"] is True
    assert report["omittedWarningCount"] == 0
    assert_chronological(report["events"])


def test_more_warnings_than_display_limit_are_counted_without_changing_check_outcome():
    backing, vocal = separated_drops(430, range(6, 426, 6))
    report = diagnose_rhythm(duration_seconds=430, accompaniment=backing, vocal=vocal)
    assert report["status"] == "needs_review"
    assert report["checks"]["backingContinuity"]["observed"]["dropoutCandidateCount"] == 70
    assert len(report["events"]) == MAX_EVENTS
    assert all(event["severity"] == "warning" for event in report["events"])
    assert report["totalEventCount"] == 70
    assert report["eventsTruncated"] is True
    assert report["omittedEventCount"] == report["omittedWarningCount"] == 6
    assert_chronological(report["events"])


def test_unlimited_report_exposes_zero_omitted_events():
    backing, vocal = separated_drops(40, [16])
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    assert report["status"] == "needs_review"
    assert report["totalEventCount"] == len(report["events"]) == 1
    assert report["eventsTruncated"] is False
    assert report["omittedEventCount"] == report["omittedWarningCount"] == 0
