"""Integration evidence for current PCM diagnostics and frozen legacy reports."""

import numpy as np
import pytest

from local_music_engine.rhythm_diagnostics import MAX_EVENTS, diagnose_rhythm
from local_music_engine.rhythm_inspection import attach_diagnostics, inspect_audio_rhythm
from local_music_engine.storage import sha256_file
from test_rhythm_context import measured_inputs
from test_rhythm_event_limits import percussion_with_many_rests
from test_signal_diagnostics import RATE, constant_gap, write_audio


def base_rhythm(duration, source_hash):
    return {"status": "unknown", "durationSeconds": duration, "findings": [],
            "segments": [], "measuredArtifactSha256": source_hash}


def test_current_inspection_detects_pcm_gap_that_legacy_v2_cannot_measure(tmp_path):
    audio = constant_gap(tmp_path)
    before = audio.read_bytes()
    older = inspect_audio_rhythm(audio, diagnostics_version="rhythm-diagnostics-v2")
    current = inspect_audio_rhythm(audio)
    assert older["diagnostics"]["version"] == "rhythm-diagnostics-v2"
    assert "mixContinuity" not in older["diagnostics"]["checks"]
    assert not any(event["category"] == "mix_dropout" for event in older["diagnostics"]["events"])
    diagnosis = current["diagnostics"]
    assert diagnosis["version"] == "rhythm-diagnostics-v4"
    assert diagnosis["status"] == current["status"] == "needs_review"
    assert diagnosis["checks"]["backingContinuity"]["status"] == "unknown"
    assert diagnosis["checks"]["mixContinuity"]["source"]["kind"] == "full_mix_pcm"
    event, = [event for event in diagnosis["events"] if event["category"] == "mix_dropout"]
    assert (event["startSeconds"], event["endSeconds"], event["severity"]) == (2.7, 3.2, "warning")
    assert event["retryEligible"] is False
    assert current["measuredArtifactSha256"] == sha256_file(audio)
    assert audio.read_bytes() == before


def test_declared_local_rest_explains_only_covered_gap_and_never_certifies_quality(tmp_path):
    samples = np.full(8 * RATE, 0.2)
    samples[2 * RATE:round(2.5 * RATE)] = 0
    samples[5 * RATE:round(5.5 * RATE)] = 0
    audio = write_audio(tmp_path / "two-gaps.wav", samples)
    report = inspect_audio_rhythm(audio, expected_sections=[
        {"startSeconds": 2, "endSeconds": 2.5, "expectedRest": True}],
        style_prompt="intentional silence, rubato, stop-time")
    events = [event for event in report["diagnostics"]["events"] if event["category"] == "mix_dropout"]
    assert [(event["startSeconds"], event["severity"]) for event in events] == [(2, "info"), (5, "warning")]
    assert events[0]["observed"]["intentAssessment"] == "consistent_with_declared_silence"
    assert events[1]["observed"]["intentAssessment"] == "unknown"
    assert all(event["observed"]["intent"] == "unknown" for event in events)
    assert report["status"] == "needs_review"
    assert report["diagnostics"]["checks"]["mixContinuity"]["status"] == "needs_review"


@pytest.mark.parametrize("version", ["rhythm-diagnostics-v1", "rhythm-diagnostics-v2"])
def test_explicit_legacy_versions_do_not_acquire_pcm_or_context_semantics(tmp_path, monkeypatch, version):
    from local_music_engine import signal_diagnostics

    def unexpected_signal_analysis(*args, **kwargs):
        raise AssertionError("A frozen legacy inspection must not run v3 PCM checks")

    monkeypatch.setattr(signal_diagnostics, "inspect_signal_dropouts", unexpected_signal_analysis)
    audio = constant_gap(tmp_path)
    source_hash = sha256_file(audio)
    percussion = {"sourceArtifactSha256": source_hash, "source": {
        "kind": "synthetic_percussion", "method": "known fixture", "reliability": 0.9},
        "pulse": {"bpm": 120, "confidence": 0.9},
        "onsetTimesSeconds": np.arange(0, 8, 0.5).tolist(),
        "beatCandidatesSeconds": np.arange(0, 8, 0.5).tolist()}
    expected = diagnose_rhythm(duration_seconds=8, percussion=percussion,
                               diagnostics_version=version)
    report = attach_diagnostics(base_rhythm(8, source_hash), audio, duration_seconds=8,
        source_sha256=source_hash, stem_inputs={"percussion": percussion}, diagnostics_version=version)
    diagnosis = report["diagnostics"]
    for key in ("checks", "events", "status", "scope", "intentPolicy", "totalEventCount", "omittedEventCount"):
        assert diagnosis[key] == expected[key]
    assert "signalVersion" not in diagnosis
    assert "contextVersion" not in diagnosis


def test_pre_finish_evidence_keeps_provenance_without_corroborating_current_pcm(tmp_path):
    envelope = np.full(256, -18.0)
    envelope[96:100] = -58.0
    inputs = measured_inputs(envelope)
    old_hash = "a" * 64
    audio = write_audio(tmp_path / "finished.wav", np.full(64_000, 0.2), rate=1000)
    current_hash = sha256_file(audio)
    for key in ("accompaniment", "vocal"):
        inputs[key]["source"]["method"] = "known pre-finish fixture"
    percussion = inputs["percussion"]
    percussion["source"]["method"] = "known pre-finish fixture"
    percussion.update(pulse={"bpm": None, "confidence": 0}, beatCandidatesSeconds=[], onsetTimesSeconds=[])
    report = attach_diagnostics(base_rhythm(64, current_hash), audio,
        duration_seconds=64, source_sha256=current_hash, backing_source_sha256=old_hash,
        stem_inputs={**inputs, "percussion": percussion})
    assert report["percussion"]["source"]["provenanceStatus"] == "source_before_linear_audio_finish"
    event, = [event for event in report["diagnostics"]["events"] if event["category"] == "backing_dropout"]
    context = event["observed"]["contextEvidence"]
    assert context["sourceBindings"]["accompaniment"] == "source_mismatch"
    assert context["sourceBindings"]["percussion"] == "source_mismatch"
    assert context.get("crossCueStatus", "unresolved") == "unresolved"
    assert event["reviewPriority"] == "insufficient_context"
    assert event["severity"] == "warning"
    assert event["retryEligible"] is False


def test_combined_limit_retains_late_pcm_warning_ahead_of_many_early_rest_observations(tmp_path):
    samples = np.full(400_000, 0.2)
    samples[380_000:380_200] = 0
    audio = write_audio(tmp_path / "late-gap.wav", samples, rate=1000)
    source_hash = sha256_file(audio)
    percussion = percussion_with_many_rests()
    percussion["sourceArtifactSha256"] = source_hash
    report = attach_diagnostics(base_rhythm(400, source_hash), audio,
        duration_seconds=400, source_sha256=source_hash, stem_inputs={"percussion": percussion})
    diagnosis = report["diagnostics"]
    assert len(diagnosis["events"]) == MAX_EVENTS
    warning, = [event for event in diagnosis["events"] if event["severity"] == "warning"]
    assert warning["category"] == "mix_dropout"
    assert warning["startSeconds"] == 380
    assert diagnosis["totalEventCount"] > MAX_EVENTS
    assert diagnosis["omittedEventCount"] == diagnosis["totalEventCount"] - MAX_EVENTS
    assert diagnosis["omittedWarningCount"] == 0
    assert diagnosis["eventsTruncated"] is True
    assert diagnosis["status"] == "needs_review"
    assert diagnosis["findings"] == diagnosis["events"]
    assert [event["startSeconds"] for event in diagnosis["events"]] == sorted(
        event["startSeconds"] for event in diagnosis["events"])


def test_pcm_warning_omissions_keep_full_check_and_never_make_a_pass(tmp_path):
    samples = np.full(150_000, 0.2)
    for index in range(70):
        first = 2000 + index * 2000
        samples[first:first + 100] = 0
    audio = write_audio(tmp_path / "many-gaps.wav", samples, rate=1000)
    source_hash = sha256_file(audio)
    # Use a genuinely unavailable percussion observation: no need to perform
    # HPSS again to exercise independent PCM aggregation.
    percussion = {"sourceArtifactSha256": source_hash, "source": {
        "kind": "percussive_estimate", "method": "unavailable", "reliability": 0},
        "pulse": {"bpm": None, "confidence": 0}, "beatCandidatesSeconds": [], "onsetTimesSeconds": []}
    report = attach_diagnostics(base_rhythm(150, source_hash), audio,
        duration_seconds=150, source_sha256=source_hash, stem_inputs={"percussion": percussion})
    diagnosis = report["diagnostics"]
    assert diagnosis["totalEventCount"] == 70
    assert len(diagnosis["events"]) == 64
    assert diagnosis["omittedEventCount"] == diagnosis["omittedWarningCount"] == 6
    assert diagnosis["checks"]["mixContinuity"]["observed"]["measuredDropoutCount"] == 70
    assert diagnosis["status"] == report["status"] == "needs_review"
