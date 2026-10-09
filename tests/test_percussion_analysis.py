"""Known transient signals verify the HPSS proxy without any model inference."""

import json
import math
import wave

import numpy as np
import pytest

from local_music_engine.music_structure import MusicStructureError
from local_music_engine.percussion_analysis import analyze_percussion


def write_audio(path, samples, *, rate=8000):
    samples = np.asarray(samples)
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    pcm = np.clip(np.rint(samples * 32768), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm.tobytes())


def drum_vocal_mix(*, seconds=18, bpm=120, rate=8000, times=None, vocal=True):
    time = np.arange(round(seconds * rate)) / rate
    # A loud sustained, mildly vibrating harmonic signal masks the quiet attacks
    # in mixture RMS. It is a vocal-like fixture, not a real voice or model stem.
    voice = (0.34 * np.sin(2 * math.pi * 220 * time + 0.25 * np.sin(2 * math.pi * 4 * time))
             + 0.14 * np.sin(2 * math.pi * 440 * time)) if vocal else np.zeros_like(time)
    drum = np.zeros_like(time)
    rng = np.random.default_rng(142)
    length = round(0.055 * rate)
    attack_time = np.arange(length) / rate
    attack = 0.12 * rng.normal(size=length) * np.exp(-attack_time * 90)
    attack += 0.14 * np.sin(2 * math.pi * 130 * attack_time) * np.exp(-attack_time * 65)
    starts = np.arange(0.3, seconds - 0.055, 60 / bpm) if times is None else times
    for start in starts:
        index = round(float(start) * rate)
        stop = min(len(drum), index + length)
        drum[index:stop] += attack[:stop - index]
    return voice + drum, [float(start) for start in starts]


def nearest_distance(values, time):
    return min((abs(value - time) for value in values), default=float("inf"))


def test_quiet_drums_under_sustained_harmonics_have_independent_timing_evidence(tmp_path):
    path = tmp_path / "mix.wav"
    samples, times = drum_vocal_mix()
    write_audio(path, samples)
    before = path.read_bytes()
    result = analyze_percussion(path)
    assert result["method"] == "hpss-percussive-proxy"
    assert result["status"] == "observed"
    assert result["pulse"]["bpm"] == pytest.approx(120, abs=3)
    assert result["pulse"]["confidence"] >= 0.6
    assert 0.5 <= result["source"]["reliability"] <= 0.7
    assert len(result["onsetTimesSeconds"]) >= len(times) - 2
    assert all(nearest_distance(result["onsetTimesSeconds"], time) <= 0.03 for time in times[1:-1])
    assert len(result["beatCandidatesSeconds"]) >= 24
    series = result["frameSeries"]
    assert series["columns"] == ["timeSeconds", "rmsDbfs", "magnitudeFlux", "harmonicRms", "percussiveRms"]
    middle = [point for point in series["points"] if 3 < point[0] < 15]
    assert np.mean([point[3] for point in middle]) > np.mean([point[4] for point in middle]) * 3
    assert path.read_bytes() == before
    assert any("cannot separate vocals" in caveat for caveat in result["limitations"])


@pytest.mark.parametrize("bpm", [84, 90, 126])
def test_off_grid_transient_periods_remain_independent_of_harmonic_voice(tmp_path, bpm):
    path = tmp_path / "non-grid.wav"
    samples, _ = drum_vocal_mix(seconds=24, bpm=bpm)
    write_audio(path, samples)
    result = analyze_percussion(path)
    assert result["pulse"]["bpm"] == pytest.approx(bpm, abs=3)
    assert result["pulse"]["confidence"] >= 0.55


def test_phase_filtered_candidates_do_not_erase_a_displaced_attack_from_raw_onsets(tmp_path):
    path = tmp_path / "jitter.wav"
    times = np.arange(0.3, 18, 0.5)
    times[10] += 0.13
    samples, starts = drum_vocal_mix(times=times)
    write_audio(path, samples)
    result = analyze_percussion(path)
    displaced = starts[10]
    assert result["pulse"]["bpm"] == pytest.approx(120, abs=3)
    assert nearest_distance(result["onsetTimesSeconds"], displaced) <= 0.03
    assert nearest_distance(result["beatCandidatesSeconds"], displaced) > 0.06
    assert len(result["onsetTimesSeconds"]) > len(result["beatCandidatesSeconds"])


def test_antiphase_stereo_preserves_the_same_transient_timing(tmp_path):
    mono_path, stereo_path = tmp_path / "mono.wav", tmp_path / "antiphase.wav"
    mono, _ = drum_vocal_mix(seconds=12)
    write_audio(mono_path, mono)
    write_audio(stereo_path, np.column_stack((mono, -mono)))
    left, right = analyze_percussion(mono_path), analyze_percussion(stereo_path)
    assert right["pulse"] == left["pulse"]
    assert right["onsetTimesSeconds"] == left["onsetTimesSeconds"]
    assert right["beatCandidatesSeconds"] == left["beatCandidatesSeconds"]


@pytest.mark.parametrize("kind", ["silence", "tone", "short"])
def test_absent_or_insufficient_percussion_is_unknown(tmp_path, kind):
    path = tmp_path / "weak.wav"
    if kind == "silence":
        samples = np.zeros(12 * 8000)
    elif kind == "tone":
        samples = 0.4 * np.sin(2 * math.pi * 220 * np.arange(12 * 8000) / 8000)
    else:
        samples, _ = drum_vocal_mix(seconds=3)
    write_audio(path, samples)
    result = analyze_percussion(path)
    assert result["status"] == "unknown"
    assert result["pulse"]["bpm"] is None
    assert result["beatCandidatesSeconds"] == []
    assert result["source"]["reliability"] == 0
    json.dumps(result, allow_nan=False)


def test_context_crosses_chunk_boundaries_and_display_arrays_are_bounded(tmp_path):
    path = tmp_path / "chunks.wav"
    times = np.arange(0.01, 26, 0.5)
    samples, _ = drum_vocal_mix(seconds=26, times=times)
    write_audio(path, samples)
    result = analyze_percussion(path)
    assert result["analysis"]["pcmBatchSeconds"] <= 2
    assert result["analysis"]["contextSeconds"] >= 0.5
    assert all(nearest_distance(result["onsetTimesSeconds"], time) <= 0.03 for time in times[1:-1])
    assert 1 <= len(result["frameSeries"]["points"]) <= 2400
    assert result["frameSeries"]["intervalSeconds"] > result["frameSeries"]["analysisHopSeconds"]
    json.dumps(result, allow_nan=False)


def test_truncated_pcm_is_rejected_instead_of_reporting_partial_evidence(tmp_path):
    path = tmp_path / "truncated.wav"
    samples, _ = drum_vocal_mix(seconds=3)
    write_audio(path, samples)
    path.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(MusicStructureError, match="truncated"):
        analyze_percussion(path)
