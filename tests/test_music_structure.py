"""Known PCM timing signals verify the advisory structure measurements."""

import json
import math
import wave
from pathlib import Path

import numpy as np
import pytest

from local_music_engine.music_structure import MusicStructureError, analyze_music_structure


def write_audio(path: Path, samples: np.ndarray, *, rate: int = 8000, width: int = 2) -> None:
    values = np.asarray(samples)
    channels = 1 if values.ndim == 1 else values.shape[1]
    maximum = 1 << (width * 8 - 1)
    integers = np.clip(np.rint(values.ravel() * maximum), -maximum, maximum - 1).astype(np.int64)
    if width == 1:
        data = (integers + 128).astype(np.uint8).tobytes()
    elif width == 3:
        unsigned = integers.astype(np.uint32)
        data = np.column_stack((unsigned & 255, unsigned >> 8 & 255, unsigned >> 16 & 255)).astype(np.uint8).tobytes()
    else:
        data = integers.astype({2: "<i2", 4: "<i4"}[width]).tobytes()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(data)


def pulses(*, bpm: float = 120, seconds: float = 16, offset: float = 0.3, rate: int = 8000,
           times: list[float] | None = None) -> np.ndarray:
    samples = np.zeros(round(rate * seconds))
    pulse_frames = round(rate * 0.05)
    time = np.arange(pulse_frames) / rate
    burst = 0.65 * np.sin(2 * math.pi * 160 * time) * np.exp(-time * 70)
    for start in np.arange(offset, seconds - 0.05, 60 / bpm) if times is None else times:
        frame = round(float(start) * rate)
        end = min(len(samples), frame + pulse_frames)
        samples[frame:end] += burst[:end - frame]
    return samples


def checks(result):
    return {finding["check"] for finding in result["findings"]}


def test_requested_bpm_does_not_force_the_estimate_or_beat_grid(tmp_path):
    path = tmp_path / "pulses.wav"
    write_audio(path, pulses(bpm=120))
    measured = analyze_music_structure(path, requested_bpm=100, time_signature="4/4")
    assert measured["version"] == "music-structure-v1"
    assert measured["estimatedBpm"] == pytest.approx(120, abs=3)
    assert measured["confidence"] >= 0.55
    assert "tempo_mismatch" in checks(measured)
    assert measured["status"] == "needs_review"
    assert all(finding["retryEligible"] is False for finding in measured["findings"])
    assert len(measured["beatTimesSeconds"]) >= 20
    assert np.median(np.diff(measured["beatTimesSeconds"])) == pytest.approx(0.5, abs=0.025)
    intervals = measured["beatIntervalMetrics"]
    assert intervals["status"] == "measured"
    assert intervals["medianIntervalSeconds"] == pytest.approx(0.5, abs=0.025)
    assert intervals["intervalMadMilliseconds"] <= 10
    assert intervals["timingDeviationP95Milliseconds"] <= 20
    other = analyze_music_structure(path, requested_bpm=150)
    assert other["estimatedBpm"] == measured["estimatedBpm"]
    assert other["beatTimesSeconds"] == measured["beatTimesSeconds"]


def test_detected_onsets_follow_audio_offset(tmp_path):
    first, second = tmp_path / "first.wav", tmp_path / "second.wav"
    write_audio(first, pulses(offset=0.3))
    write_audio(second, pulses(offset=0.48))
    left, right = analyze_music_structure(first), analyze_music_structure(second)
    assert left["estimatedBpm"] == right["estimatedBpm"]
    assert right["beatTimesSeconds"][0] - left["beatTimesSeconds"][0] == pytest.approx(0.18, abs=0.025)


def test_half_double_tempo_is_ambiguous_instead_of_claiming_wrong_tempo(tmp_path):
    path = tmp_path / "pulses.wav"
    write_audio(path, pulses(bpm=120))
    result = analyze_music_structure(path, requested_bpm=60)
    assert result["estimatedBpm"] == pytest.approx(120, abs=3)
    assert "tempo_ambiguity" in checks(result)
    assert "tempo_mismatch" not in checks(result)
    assert result["status"] == "needs_review"


@pytest.mark.parametrize("bpm", [84, 90, 108, 126])
def test_non_integer_frame_periods_do_not_favor_a_long_period_alias(tmp_path, bpm):
    path = tmp_path / "non-grid.wav"
    write_audio(path, pulses(bpm=bpm, seconds=24))
    result = analyze_music_structure(path, requested_bpm=bpm)
    assert result["estimatedBpm"] == pytest.approx(bpm, abs=3)
    assert result["confidence"] >= 0.55
    assert "tempo_mismatch" not in checks(result)


def test_bar_rate_period_is_ambiguous_with_four_times_requested_tempo(tmp_path):
    path = tmp_path / "bar-pulses.wav"
    write_audio(path, pulses(bpm=30, seconds=40))
    result = analyze_music_structure(path, requested_bpm=120)
    assert result["estimatedBpm"] == pytest.approx(30, abs=1)
    assert "tempo_ambiguity" in checks(result)
    assert "tempo_mismatch" not in checks(result)
    assert "quarter-note" in result["confidenceMeaning"]


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_pcm_widths_and_antiphase_stereo_retain_rhythm(tmp_path, width):
    path = tmp_path / "stereo.wav"
    mono = pulses()
    write_audio(path, np.column_stack((mono, -mono)), width=width)
    result = analyze_music_structure(path, requested_bpm=120)
    assert result["estimatedBpm"] == pytest.approx(120, abs=3)
    assert "tempo_mismatch" not in checks(result)
    assert max(point[2] for point in result["frameSeries"]["points"]) > 0.3


def test_rhythm_in_one_channel_survives_a_silent_other_channel(tmp_path):
    path = tmp_path / "one-channel.wav"
    mono = pulses()
    write_audio(path, np.column_stack((np.zeros_like(mono), mono)))
    result = analyze_music_structure(path)
    assert result["estimatedBpm"] == pytest.approx(120, abs=3)


@pytest.mark.parametrize("kind", ["silence", "tone", "sparse", "short"])
def test_insufficient_rhythm_is_unknown_without_tempo_warning(tmp_path, kind):
    path = tmp_path / "weak.wav"
    if kind == "silence":
        samples = np.zeros(16 * 8000)
    elif kind == "tone":
        samples = 0.3 * np.sin(2 * math.pi * 220 * np.arange(16 * 8000) / 8000)
    elif kind == "sparse":
        samples = pulses(times=[2, 5, 9, 13])
    else:
        samples = pulses(seconds=3)
    write_audio(path, samples)
    result = analyze_music_structure(path, requested_bpm=120)
    assert result["status"] == "unknown"
    assert result["estimatedBpm"] is None
    assert result["beatTimesSeconds"] == []
    assert not {"tempo_mismatch", "tempo_drift", "tempo_ambiguity"} & checks(result)
    json.dumps(result, allow_nan=False)
    if kind == "silence":
        assert all(point[1] is None for point in result["frameSeries"]["points"])


def test_local_tempo_change_is_detected_and_declared_rubato_is_advisory(tmp_path):
    path = tmp_path / "changing.wav"
    onset_times = [*np.arange(0.3, 16, 0.5), *np.arange(16.3, 32, 0.4)]
    write_audio(path, pulses(seconds=32, times=onset_times))
    result = analyze_music_structure(path, requested_bpm=120)
    local = [item["estimatedBpm"] for item in result["segments"] if item["estimatedBpm"] is not None]
    assert min(local) == pytest.approx(120, abs=3)
    assert max(local) == pytest.approx(150, abs=3)
    assert "tempo_drift" in checks(result)
    assert "tempo_mismatch" not in checks(result)
    rubato = analyze_music_structure(path, style_prompt="rubato with intentional tempo changes")
    assert next(item for item in rubato["findings"] if item["check"] == "tempo_drift")["severity"] == "info"


def test_local_drift_is_detected_without_a_confident_whole_track_tempo(tmp_path):
    path = tmp_path / "unstable.wav"
    onset_times = [*np.arange(0.3, 15, 0.5), *np.arange(15.3, 33, 0.6)]
    write_audio(path, pulses(seconds=33, times=onset_times))
    result = analyze_music_structure(path, requested_bpm=120)
    assert result["estimatedBpm"] is None
    drift = next(item for item in result["findings"] if item["check"] == "tempo_drift")
    assert drift["observed"]["minimumBpm"] == pytest.approx(100, abs=3)
    assert drift["observed"]["maximumBpm"] == pytest.approx(120, abs=3)
    assert drift["confidence"] >= 0.55
    assert drift["retryEligible"] is False
    assert result["status"] == "needs_review"


def test_local_bar_and_beat_rate_aliases_do_not_claim_tempo_drift(tmp_path):
    path = tmp_path / "bar-then-beat.wav"
    times = [*np.arange(0.3, 24, 2.0), *np.arange(24.3, 48, 0.5)]
    write_audio(path, pulses(seconds=48, times=times))
    result = analyze_music_structure(path, requested_bpm=120)
    observed = [item["estimatedBpm"] for item in result["segments"] if item["estimatedBpm"] is not None]
    assert any(abs(value - 30) < 1 for value in observed)
    assert any(abs(value - 120) < 3 for value in observed)
    assert "tempo_drift" not in checks(result)


def test_explicit_tempo_variation_makes_requested_tempo_difference_information(tmp_path):
    path = tmp_path / "rubato.wav"
    write_audio(path, pulses(bpm=120))
    result = analyze_music_structure(path, requested_bpm=100, style_prompt="rubato")
    assert next(item for item in result["findings"] if item["check"] == "tempo_mismatch")["severity"] == "info"
    assert result["status"] == "observed"


def test_six_eight_is_three_quarter_notes_and_downbeats_are_unverified(tmp_path):
    path = tmp_path / "pulses.wav"
    write_audio(path, pulses())
    meter = analyze_music_structure(path, requested_bpm=120, time_signature="6/8")["meter"]
    assert meter["status"] == "projected"
    assert meter["timeSignature"] == "6/8"
    assert meter["quarterNotesPerBar"] == 3
    assert meter["projectedBarDurationSeconds"] == 1.5
    assert "no downbeat" in meter["caveat"]


def test_inferred_pulse_does_not_invent_quarter_note_bpm_for_meter_projection(tmp_path):
    path = tmp_path / "bar-pulses.wav"
    write_audio(path, pulses(bpm=30, seconds=40))
    result = analyze_music_structure(path, time_signature="4/4")
    assert result["estimatedBpm"] is not None
    assert result["meter"]["quarterNotesPerBar"] == 4
    assert result["meter"]["status"] == "unknown"
    assert result["meter"]["projectedBarDurationSeconds"] is None


def test_frame_rms_spectrum_and_shift_warnings_are_listening_observations(tmp_path):
    path = tmp_path / "shift.wav"
    rate = 16_000
    time = np.arange(16 * rate) / rate
    samples = np.where(time < 8, 0.08 * np.sin(2 * math.pi * 220 * time),
                       0.4 * np.sin(2 * math.pi * 5000 * time))
    write_audio(path, samples, rate=rate)
    result = analyze_music_structure(path)
    assert {"loudness_shift", "spectral_shift"} <= checks(result)
    assert all(item["retryEligible"] is False for item in result["findings"])
    points = result["frameSeries"]["points"]
    quiet = next(point for point in points if 3 < point[0] < 4)
    loud = next(point for point in points if 11 < point[0] < 12)
    assert quiet[1] == pytest.approx(20 * math.log10(0.08 / math.sqrt(2)), abs=0.15)
    assert quiet[3] == pytest.approx(220, abs=6)
    assert loud[3] == pytest.approx(5000, abs=6)
    assert quiet[4] > 0.9 and loud[6] > 0.9


def test_frame_series_is_bounded_and_preserves_json_safety(tmp_path):
    path = tmp_path / "long.wav"
    write_audio(path, pulses(seconds=70))
    result = analyze_music_structure(path)
    series = result["frameSeries"]
    assert 1 <= len(series["points"]) <= 2400
    assert series["intervalSeconds"] > series["analysisHopSeconds"]
    assert len(series["columns"]) == len(series["points"][0])
    json.dumps(result, allow_nan=False)


def test_final_partial_analysis_window_retains_last_sample_peak(tmp_path):
    path = tmp_path / "tail.wav"
    samples = np.zeros(8000)
    samples[-10:] = 20_000 / 32768
    write_audio(path, samples)
    result = analyze_music_structure(path)
    assert max(point[2] for point in result["frameSeries"]["points"]) == pytest.approx(20_000 / 32768, abs=1e-6)
    assert result["frameSeries"]["points"][-1][0] > 0.96


def test_observed_interval_metrics_account_for_an_intentional_missing_pulse(tmp_path):
    path = tmp_path / "rest.wav"
    times = [float(value) for value in np.arange(0.3, 16, 0.5) if not 5 < value < 6]
    write_audio(path, pulses(times=times))
    result = analyze_music_structure(path)
    intervals = result["beatIntervalMetrics"]
    assert intervals["status"] == "measured"
    assert intervals["skippedPulseCount"] >= 1
    assert intervals["medianIntervalSeconds"] == pytest.approx(0.5, abs=0.025)
    assert intervals["timingDeviationP95Milliseconds"] < 25


def test_truncated_pcm_and_incomplete_multichannel_frame_are_rejected(tmp_path):
    path = tmp_path / "broken.wav"
    write_audio(path, pulses())
    path.write_bytes(path.read_bytes()[:-400])
    with pytest.raises(MusicStructureError, match="truncated"):
        analyze_music_structure(path)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0" * 399)
    with pytest.raises(MusicStructureError, match="incomplete frame"):
        analyze_music_structure(path)


@pytest.mark.parametrize("requested", [True, float("nan"), float("inf"), 0, 301])
def test_invalid_requested_bpm_is_rejected(tmp_path, requested):
    with pytest.raises(MusicStructureError, match="BPM"):
        analyze_music_structure(tmp_path / "missing.wav", requested_bpm=requested)
