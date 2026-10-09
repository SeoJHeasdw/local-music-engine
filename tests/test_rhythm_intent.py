"""Declared plans must explain the measured kind and the whole affected range."""

from copy import deepcopy

import numpy as np
import pytest

from local_music_engine.rhythm_diagnostics import LEGACY_VERSION, VERSION, diagnose_rhythm, validate_expected_sections
from test_rhythm_diagnostics import only, percussion, stems


def jitter():
    beats = np.arange(0.0, 40.0, 0.5)
    beats[32:40] += np.array([0, 0.08, -0.05, 0.065, -0.07, 0.09, -0.08, 0])
    return percussion(beats)


def tempo_change():
    beats = np.r_[np.arange(0, 12, 0.5), np.arange(12, 42, 0.6)]
    segments = [{"startSeconds": 0, "endSeconds": 12, "bpm": 120, "confidence": 0.9},
                {"startSeconds": 12, "endSeconds": 24, "bpm": 100, "confidence": 0.9},
                {"startSeconds": 24, "endSeconds": 42, "bpm": 100, "confidence": 0.9}]
    return percussion(beats, segments=segments)


@pytest.mark.parametrize("style", ["expressive rubato", "steady drums, no rubato", "박자 변화가 있는 곡", "tempo changes"])
def test_whole_song_words_do_not_hide_measured_jitter(style):
    report = diagnose_rhythm(duration_seconds=40, percussion=jitter(), style_prompt=style)
    event, = only(report, "beat_timing")
    assert report["version"] == VERSION
    assert report["status"] == "needs_review"
    assert event["severity"] == "warning"
    assert event["observed"]["intent"] == "unknown"
    assert event["retryEligible"] is False


@pytest.mark.parametrize("intent", ["rubato", "tempo_change"])
def test_scoped_tempo_plan_cannot_hide_irregular_timing(intent):
    section = {"startSeconds": 14, "endSeconds": 22, "timingIntent": intent}
    report = diagnose_rhythm(duration_seconds=40, percussion=jitter(), expected_sections=[section])
    event, = only(report, "beat_timing")
    assert event["severity"] == "warning"
    assert event["observed"]["intentAssessment"] == "irregular_timing_not_explained_by_tempo_plan"
    assert report["checks"]["beatTiming"]["observed"]["localIntentMatchedCount"] == 0


@pytest.mark.parametrize("intent", ["rubato", "tempo_change"])
def test_compatible_tempo_plan_is_informational_but_not_human_approved(intent):
    section = {"startSeconds": 0, "endSeconds": 24, "timingIntent": intent}
    before = deepcopy(section)
    report = diagnose_rhythm(duration_seconds=42, percussion=tempo_change(), expected_sections=[section])
    event, = only(report, "beat_timing")
    assert event["severity"] == "info"
    assert event["observed"]["intentAssessment"] == "consistent_with_local_tempo_plan"
    assert event["observed"]["intent"] == "unknown"
    assert event["retryEligible"] is False
    assert report["checks"]["beatTiming"]["status"] == "observed"
    assert report["status"] == "unknown"  # Missing independent backing evidence.
    assert section == before


@pytest.mark.parametrize("section", [
    {"startSeconds": 12, "endSeconds": 24, "timingIntent": "tempo_change"},
    {"startSeconds": 24, "endSeconds": 42, "timingIntent": "tempo_change"},
    {"startSeconds": 0, "endSeconds": 24, "name": "bridge"},
])
def test_partial_or_named_region_does_not_excuse_tempo_departure(section):
    report = diagnose_rhythm(duration_seconds=42, percussion=tempo_change(), expected_sections=[section])
    event, = only(report, "beat_timing")
    assert event["severity"] == "warning"
    assert report["status"] == "needs_review"


def test_rest_plan_must_cover_the_entire_measured_backing_dip():
    backing, vocal = stems(start=16, end=18)
    kwargs = {"duration_seconds": 40, "percussion": percussion(), "accompaniment": backing, "vocal": vocal}
    matching = diagnose_rhythm(**kwargs, expected_sections=[{"startSeconds": 16, "endSeconds": 18, "expectedRest": True}])
    assert only(matching, "arrangement_break")[0]["severity"] == "info"
    for section in [{"startSeconds": 16.1, "endSeconds": 18, "expectedRest": True},
                    {"startSeconds": 16, "endSeconds": 17.9, "accompanimentExpected": False},
                    {"startSeconds": 16, "endSeconds": 18, "timingIntent": "rubato"}]:
        assert only(diagnose_rhythm(**kwargs, expected_sections=[section]), "backing_dropout")[0]["severity"] == "warning"


@pytest.mark.parametrize("section", [
    {"startSeconds": True, "endSeconds": 18, "expectedRest": True},
    {"startSeconds": 16, "endSeconds": float("nan"), "expectedRest": True},
    {"startSeconds": -1, "endSeconds": 18, "expectedRest": True},
    {"startSeconds": 16, "endSeconds": 41, "expectedRest": True},
    {"startSeconds": 18, "endSeconds": 16, "expectedRest": True},
    {"startSeconds": 16, "endSeconds": 18, "expectedRest": "true"},
    {"startSeconds": 16, "endSeconds": 18, "timingIntent": "jazz"},
    {"startSeconds": 16, "endSeconds": 18, "ignoreAllErrors": True},
])
def test_invalid_intent_cannot_silently_remove_warnings(section):
    with pytest.raises(ValueError, match="Intent|intent|must be"):
        diagnose_rhythm(duration_seconds=40, percussion=jitter(), expected_sections=[section])


def test_local_intent_does_not_invent_missing_measurements():
    report = diagnose_rhythm(duration_seconds=40, expected_sections=[
        {"startSeconds": 0, "endSeconds": 40, "timingIntent": "rubato", "expectedRest": True}])
    assert report["status"] == "unknown"
    assert not report["events"]
    assert validate_expected_sections(None, 40) == []


def test_measured_pcm_tempo_plan_contextualizes_both_broad_and_detailed_findings(tmp_path):
    from local_music_engine.rhythm_diagnostics import PREVIOUS_VERSION
    from local_music_engine.rhythm_inspection import inspect_audio_rhythm
    from local_music_engine.storage import sha256_file
    from test_music_structure import pulses, write_audio

    audio = tmp_path / "changing-pulse.wav"
    times = np.r_[np.arange(0.3, 12, 0.5), np.arange(12.3, 41.9, 0.6)]
    write_audio(audio, pulses(seconds=42, times=times.tolist()))
    # Known fixture attack times accompany a genuine full-mixture DSP read.
    source = tempo_change()
    source["sourceArtifactSha256"] = sha256_file(audio)
    # This fixture contains deliberately gated metronome pulses. Verify the
    # frozen v2 tempo contract independently of v3's full-mix silence scope.
    kwargs = {"stem_inputs": {"percussion": source}, "diagnostics_version": PREVIOUS_VERSION}
    baseline = inspect_audio_rhythm(audio, **kwargs)
    broad, = [finding for finding in baseline["findings"] if finding["check"] == "tempo_drift"]
    assert broad["severity"] == "warning"
    planned = inspect_audio_rhythm(audio, **kwargs, expected_sections=[
        {"startSeconds": 0, "endSeconds": 42, "timingIntent": "tempo_change"}])
    broad_planned, = [finding for finding in planned["findings"] if finding["check"] == "tempo_drift"]
    assert broad_planned["severity"] == "info"
    assert broad_planned["observed"]["rangePercent"] == broad["observed"]["rangePercent"]
    assert only(planned["diagnostics"], "beat_timing")[0]["severity"] == "info"
    assert planned["status"] == "unknown"  # No independent backing evidence.
    assert baseline["status"] == "needs_review"


def test_local_tempo_plan_preserves_unrelated_warnings_and_partial_broad_observations():
    from local_music_engine.rhythm_diagnostics import contextualize_tempo_observations

    original = {"status": "needs_review", "estimatedBpm": 120, "findings": [
        {"check": "tempo_drift", "severity": "warning", "startSeconds": 0, "endSeconds": 40,
         "observed": {"rangePercent": 20}},
        {"check": "tempo_mismatch", "severity": "warning", "startSeconds": None, "endSeconds": None,
         "observed": {"differencePercent": 20}},
    ]}
    partial = contextualize_tempo_observations(original, [{"startSeconds": 0, "endSeconds": 24, "timingIntent": "rubato"}])
    assert partial["findings"][0]["severity"] == "warning"
    full = contextualize_tempo_observations(original, [{"startSeconds": 0, "endSeconds": 40, "timingIntent": "rubato"}])
    assert full["findings"][0]["severity"] == "info"
    assert full["findings"][1]["severity"] == "warning"
    assert full["status"] == "needs_review"
    assert original["findings"][0]["severity"] == "warning"


@pytest.mark.parametrize("check", ["tempo_mismatch", "tempo_drift"])
def test_v2_restores_base_warnings_hidden_by_whole_song_style_words(check):
    from local_music_engine.rhythm_diagnostics import contextualize_tempo_observations

    base = {"status": "observed", "estimatedBpm": 120, "findings": [
        {"check": check, "severity": "info", "startSeconds": 0, "endSeconds": 40, "observed": {"rangePercent": 20}},
    ]}
    result = contextualize_tempo_observations(base, [])
    assert result["status"] == "needs_review"
    assert result["findings"][0]["severity"] == "warning"
    assert base["findings"][0]["severity"] == "info"


def test_actual_pcm_requested_tempo_difference_is_not_hidden_by_no_rubato(tmp_path):
    from local_music_engine.rhythm_inspection import inspect_audio_rhythm
    from test_music_structure import pulses, write_audio

    audio = tmp_path / "wrong-requested-tempo.wav"
    write_audio(audio, pulses(bpm=120, seconds=16))
    current = inspect_audio_rhythm(audio, requested_bpm=100, style_prompt="steady drums, no rubato")
    mismatch, = [finding for finding in current["findings"] if finding["check"] == "tempo_mismatch"]
    assert mismatch["severity"] == "warning"
    assert current["status"] == "needs_review"
    legacy = inspect_audio_rhythm(audio, requested_bpm=100, style_prompt="steady drums, no rubato",
                                  diagnostics_version=LEGACY_VERSION)
    old, = [finding for finding in legacy["findings"] if finding["check"] == "tempo_mismatch"]
    assert old["severity"] == "info"


def test_unknown_diagnostic_version_is_rejected():
    with pytest.raises(ValueError, match="version"):
        diagnose_rhythm(duration_seconds=40, diagnostics_version="future-unvalidated")


@pytest.mark.parametrize("version", [LEGACY_VERSION, VERSION])
def test_generation_and_finished_audio_keep_the_frozen_diagnosis_version(tmp_path, monkeypatch, version):
    from local_music_engine import auto_quality
    from local_music_engine.storage import ProjectStore
    from local_music_engine.workflow import generate_candidates, resume_latest_batch
    from test_auto_quality import Reports
    from test_rhythm_workflow import CleanEdgesClient
    from test_workflow import FakeAceClient

    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    store = ProjectStore.initialize(tmp_path / "song", title="버전 보존 검사", lyrics="[Instrumental]",
        style_prompt="rubato piano", target_duration_seconds=10)
    policy = auto_quality.quality_policy("audio", max_attempts=1)
    policy["diagnosticsVersion"] = version
    seen = []
    original = auto_quality.attach_diagnostics

    def measure(*args, **kwargs):
        seen.append(kwargs["diagnostics_version"])
        return original(*args, **kwargs)

    monkeypatch.setattr(auto_quality, "attach_diagnostics", measure)
    result = generate_candidates(store.root, seeds=[19], quality=policy, quality_backend=Reports(),
                                 client_factory=CleanEdgesClient, engine="ace-step")
    assert seen == [version, version]
    project = store.load()
    assert all(candidate["quality"]["rhythm"]["diagnostics"]["version"] == version for candidate in project["candidates"])
    assert all(candidate["humanReview"]["status"] == "unreviewed" for candidate in project["candidates"])
    assert all(store.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    again = resume_latest_batch(store.root, client_factory=CleanEdgesClient, quality_backend=Reports(), engine="ace-step")
    assert seen == [version, version]
    assert again["candidateIds"] == result["candidateIds"]
