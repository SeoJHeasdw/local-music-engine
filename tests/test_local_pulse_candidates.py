"""Local timing support must reach diagnosis without inventing whole-track beats."""

import importlib.util
from pathlib import Path
import wave

import numpy as np
import pytest

from local_music_engine.music_structure import _observed_beats, _tempo
from local_music_engine.percussion_analysis import _supported_beat_candidates, analyze_percussion
from local_music_engine.rhythm_diagnostics import diagnose_rhythm


WORKER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "quality_worker.py"
spec = importlib.util.spec_from_file_location("local_pulse_quality_worker", WORKER_PATH)
worker = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(worker)


def partly_periodic_onsets():
    rng = np.random.default_rng(13)
    times = []
    current = 0.3
    while current < 24:
        times.append(current)
        current += rng.uniform(0.2, 0.8)
    stable = np.arange(24.3, 48, 0.5)
    stable[20:28] += np.array([0, 0.08, -0.05, 0.065, -0.07, 0.09, -0.08, 0])
    times.extend(stable)
    current = 48.3
    while current < 59.9:
        times.append(current)
        current += rng.uniform(0.2, 0.8)
    return np.asarray(times), float(stable[21])


def percussion_pcm(times, *, rate, start=0.0, end=60.0):
    """Generate known noise/kick attacks; no model or separated stem is loaded."""
    samples = np.zeros(round((end - start) * rate), dtype=np.float32)
    attack_time = np.arange(round(0.055 * rate)) / rate
    attack = 0.12 * np.random.default_rng(142).normal(size=len(attack_time)) * np.exp(-attack_time * 90)
    attack += 0.14 * np.sin(2 * np.pi * 130 * attack_time) * np.exp(-attack_time * 65)
    for time in times:
        index = round((float(time) - start) * rate)
        first, stop = max(0, index), min(len(samples), index + len(attack))
        if first < stop:
            samples[first:stop] += attack[first - index:stop - index]
    return samples


@pytest.mark.parametrize("method", ["hpss", "worker"])
def test_pcm_adjacent_local_support_reaches_jitter_diagnosis_when_global_pulse_is_absent(tmp_path, method):
    times, displaced = partly_periodic_onsets()
    if method == "hpss":
        path = tmp_path / "local-pulse.wav"
        pcm = np.rint(percussion_pcm(times, rate=8000) * 32768).astype("<i2")
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(pcm.tobytes())
        result = analyze_percussion(path)
    else:
        batches, previous = [], None
        for start in range(0, 60, 2):
            context_start, context_end = max(0, start - 0.1), min(60, start + 2.1)
            mono = percussion_pcm(times, rate=44100, start=context_start, end=context_end)
            measured, previous = worker.drum_frame_features(np.column_stack((mono, -mono)),
                offset=context_start, core_start=start, core_end=start + 2, previous=previous)
            batches.append(measured)
        result = worker.drum_timing_summary(np.concatenate(batches), duration=60, source_hash="synthetic-pcm")
        assert result["sourceArtifactSha256"] == "synthetic-pcm"

    assert result["status"] == "unknown"
    assert result["pulse"]["bpm"] is None
    assert result["pulse"]["confidence"] < 0.55
    supported = [segment for segment in result["segments"] if segment["bpm"] is not None]
    assert [segment["startSeconds"] for segment in supported] == [24, 36]
    assert all(segment["confidence"] >= 0.6 for segment in supported)
    candidates, onsets = result["beatCandidatesSeconds"], result["onsetTimesSeconds"]
    assert len(candidates) >= 40
    assert all(24 <= time < 48 for time in candidates)
    assert set(candidates) <= set(onsets)
    assert min(abs(time - displaced) for time in onsets) <= 0.03
    # The same measured PCM reaches the real consumer, including the interval
    # disturbance that previously disappeared when global bpm was absent.
    report = diagnose_rhythm(duration_seconds=60, percussion=result)
    check = report["checks"]["beatTiming"]
    assert check["status"] == "needs_review"
    assert check["observed"]["wholeTrackPulseSupported"] is False
    event, = [event for event in report["events"] if event["category"] == "beat_timing"]
    assert 24 <= event["startSeconds"] < event["endSeconds"] <= 48
    assert event["observed"]["timingDeviationP95Milliseconds"] > 100
    assert event["observed"]["missingPulseCount"] == 0
    assert event["retryEligible"] is False


def observed_features(seconds=60):
    features = np.zeros((seconds * 100, 8))
    features[:, 0] = np.arange(len(features)) / 100
    features[np.arange(30, len(features), 50), 7] = 1
    return features, _tempo(features[:, 7], 0.01)


def segment(start, bpm=120, confidence=0.9):
    return {"startSeconds": start, "endSeconds": start + 12, "bpm": bpm, "confidence": confidence}


@pytest.mark.parametrize("global_alias", [60, 240])
def test_weak_global_alias_cannot_phase_filter_supported_local_pulse(global_alias):
    features, tempo = observed_features(24)
    weak = {**tempo, "bpm": global_alias, "confidence": 0.58}
    candidates = _supported_beat_candidates(features, weak, [segment(0), segment(12)])
    assert candidates == features[tempo["peaks"], 0].tolist()
    assert np.median(np.diff(candidates)) == pytest.approx(0.5)


def test_supported_global_alias_keeps_its_own_candidate_track():
    features, tempo = observed_features(24)
    global_pulse = {**tempo, "bpm": 60, "confidence": 0.9}
    candidates = _supported_beat_candidates(features, global_pulse, [segment(0), segment(12)])
    assert candidates == _observed_beats(features, global_pulse)
    assert np.median(np.diff(candidates)) == pytest.approx(1.0)


@pytest.mark.parametrize("segments", [[segment(0)], [segment(0), segment(12, confidence=0.59)],
    [segment(0), segment(12.1)], [segment(0), segment(12, bpm=60)]])
def test_one_weak_discontinuous_or_different_alias_segment_cannot_make_a_local_track(segments):
    features, tempo = observed_features(36)
    assert _supported_beat_candidates(features, {**tempo, "bpm": None}, segments) == []


def test_supported_groups_do_not_create_candidates_across_an_unsupported_range():
    features, tempo = observed_features()
    segments = [segment(0), segment(12), segment(24, confidence=0.4), segment(36), segment(48)]
    candidates = _supported_beat_candidates(features, {**tempo, "bpm": None}, segments)
    assert candidates
    assert all(time < 24 or time >= 36 for time in candidates)
    assert set(candidates) <= set(features[tempo["peaks"], 0])


def test_stepwise_drift_cannot_accumulate_into_one_stable_local_phase_reference():
    features, tempo = observed_features(36)
    segments = [segment(0, bpm=120), segment(12, bpm=125), segment(24, bpm=130)]
    candidates = _supported_beat_candidates(features, {**tempo, "bpm": None}, segments)
    assert candidates
    assert all(time < 24 for time in candidates)
