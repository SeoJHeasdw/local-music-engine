"""The current diagnosis measures hit timing against the track's own repetitions.

Earlier frozen diagnosis versions must stay exactly as they were, and a hit
timing measurement that fails or finds no repetition is unknown, never a pass.
"""

import numpy as np
import pytest

from local_music_engine import rhythm_inspection
from local_music_engine.rhythm_diagnostics import SIGNAL_VERSION, VERSION
from local_music_engine.rhythm_inspection import inspect_audio_rhythm
from local_music_engine.storage import sha256_file
from test_timbre_tracking import bump, displaced, pattern, render, triangle, write_audio


def audio(tmp_path, name, events):
    return write_audio(tmp_path / f"{name}.wav", render(events))


def timing_events(report):
    return [event for event in report["diagnostics"]["events"] if event["category"] == "beat_timing"]


def test_sustained_hit_displacement_reaches_the_diagnosis_findings_and_status(tmp_path):
    path = audio(tmp_path, "wobble", displaced(lambda time: triangle(time, 16, 22, 0.06)))
    before = path.read_bytes()
    report = inspect_audio_rhythm(path)
    diagnosis = report["diagnostics"]
    assert diagnosis["version"] == VERSION == "rhythm-diagnostics-v4"
    assert diagnosis["hitTimingVersion"] == report["hitTiming"]["version"] == "timbre-timing-v2"
    assert diagnosis["checks"]["hitTiming"]["status"] == "needs_review"
    assert diagnosis["checks"]["hitTiming"]["source"]["kind"] == "full_mix_pcm"
    event, = [event for event in timing_events(report) if event["severity"] == "warning"]
    assert event["check"] == "repeated_hit_timing_shift_suspected"
    assert 15.5 <= event["startSeconds"] <= 17 and 21 <= event["endSeconds"] <= 22.5
    assert event["retryEligible"] is False and event["evidenceStatus"] == "intent_unknown"
    assert diagnosis["status"] == report["status"] == "needs_review"
    assert event in report["findings"]
    # Measured hits, the fitted expectation and model predictions stay distinct.
    timing = report["hitTiming"]
    assert timing["sourceArtifactSha256"] == report["measuredArtifactSha256"] == sha256_file(path)
    assert timing["modelInference"] is False and timing["modelPredictedBeatTimesSeconds"] == []
    assert event["observed"]["measuredOnsetTimesSeconds"] != event["observed"]["expectedTimesSeconds"]
    assert path.read_bytes() == before


def test_steady_rhythm_is_observed_without_a_timing_finding(tmp_path):
    report = inspect_audio_rhythm(audio(tmp_path, "steady", pattern()))
    assert not timing_events(report)
    assert report["diagnostics"]["checks"]["hitTiming"]["status"] == "observed"
    assert report["hitTiming"]["supportedFraction"] >= 0.9


def test_previous_diagnosis_version_is_unchanged_and_has_no_hit_timing(tmp_path):
    path = audio(tmp_path, "wobble", displaced(lambda time: triangle(time, 16, 22, 0.06)))
    report = inspect_audio_rhythm(path, diagnostics_version=SIGNAL_VERSION)
    assert report["diagnostics"]["version"] == "rhythm-diagnostics-v3"
    assert "hitTiming" not in report and "hitTiming" not in report["diagnostics"]["checks"]
    assert "hitTimingVersion" not in report["diagnostics"]
    assert not any(event["check"] == "repeated_hit_timing_shift_suspected" for event in report["diagnostics"]["events"])


def test_declared_tempo_plan_explains_a_gradual_change_but_never_irregular_timing(tmp_path):
    plan = [{"startSeconds": 14, "endSeconds": 24, "timingIntent": "tempo_change"}]
    gradual = inspect_audio_rhythm(audio(tmp_path, "gradual", displaced(lambda time: bump(time, 15, 23, 0.2))),
                                   expected_sections=plan)
    event, = timing_events(gradual)
    assert event["check"] == "smooth_timing_change_observed" and event["severity"] == "info"
    assert event["observed"]["intentAssessment"] == "consistent_with_local_tempo_plan"
    assert gradual["diagnostics"]["checks"]["hitTiming"]["status"] == "observed"
    assert gradual["diagnostics"]["checks"]["hitTiming"]["observed"]["localIntentMatchedCount"] == 1
    irregular = inspect_audio_rhythm(audio(tmp_path, "irregular", displaced(lambda time: triangle(time, 16, 22, 0.06))),
                                     expected_sections=plan)
    warning, = [event for event in timing_events(irregular) if event["severity"] == "warning"]
    assert warning["observed"]["intentAssessment"] == "irregular_timing_not_explained_by_tempo_plan"
    assert irregular["diagnostics"]["checks"]["hitTiming"]["status"] == "needs_review"


def test_gradual_change_without_a_plan_is_still_only_an_observation(tmp_path):
    report = inspect_audio_rhythm(audio(tmp_path, "gradual", displaced(lambda time: bump(time, 15, 23, 0.2))))
    event, = timing_events(report)
    assert event["severity"] == "info" and "intentAssessment" not in event["observed"]
    assert report["diagnostics"]["checks"]["hitTiming"]["status"] == "observed"


def test_unrepeated_or_unmeasurable_audio_leaves_hit_timing_unknown(tmp_path, monkeypatch):
    constant = write_audio(tmp_path / "constant.wav", np.full(8 * 8000, 0.2))
    report = inspect_audio_rhythm(constant)
    check = report["diagnostics"]["checks"]["hitTiming"]
    assert check["status"] == "unknown" and not timing_events(report)
    assert report["diagnostics"]["status"] != "observed"

    def broken(_audio):
        raise RuntimeError("deliberate measurement failure")

    monkeypatch.setattr(rhythm_inspection, "analyze_timbre_timing", broken)
    failed = inspect_audio_rhythm(audio(tmp_path, "steady", pattern()))
    check = failed["diagnostics"]["checks"]["hitTiming"]
    assert check["status"] == "unknown" and "deliberate measurement failure" in check["reason"]
    assert failed["hitTiming"]["events"] == [] and failed["diagnostics"]["status"] != "observed"


def test_hit_timing_bound_to_other_audio_is_rejected(tmp_path, monkeypatch):
    path = audio(tmp_path, "steady", pattern())
    original = rhythm_inspection.analyze_timbre_timing
    monkeypatch.setattr(rhythm_inspection, "analyze_timbre_timing",
                        lambda audio_path: {**original(audio_path), "sourceArtifactSha256": "f" * 64})
    with pytest.raises(ValueError, match="not bound to the current audio"):
        inspect_audio_rhythm(path)
