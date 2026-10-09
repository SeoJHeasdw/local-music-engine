"""Streaming full-mix digital silence is evidence, never a certified model fault."""

from pathlib import Path
import wave

import numpy as np
import pytest

from local_music_engine import signal_diagnostics
from local_music_engine.signal_diagnostics import inspect_signal_dropouts
from local_music_engine.storage import sha256_file


RATE = 8000


def write_audio(path, samples, *, rate=RATE, width=2):
    samples = np.asarray(samples)
    if samples.ndim == 1:
        samples = samples[:, None]
    scale = 1 << (width * 8 - 1)
    integers = np.rint(np.clip(samples, -1, 1 - 1 / scale) * scale).astype(np.int64)
    if width == 1:
        pcm = (integers + 128).astype(np.uint8).tobytes()
    elif width == 3:
        unsigned = integers.reshape(-1) & 0xFFFFFF
        pcm = np.column_stack((unsigned & 255, (unsigned >> 8) & 255, (unsigned >> 16) & 255)).astype(np.uint8).tobytes()
    else:
        pcm = integers.astype({2: "<i2", 4: "<i4"}[width]).tobytes()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(samples.shape[1])
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(pcm)
    return path


def constant_gap(tmp_path, *, start=2.7, end=3.2, channels=1, width=2):
    samples = np.full((8 * RATE, channels), 0.2)
    samples[round(start * RATE):round(end * RATE)] = 0
    return write_audio(tmp_path / "gap.wav", samples, width=width)


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_interior_digital_gap_is_measured_without_modifying_pcm(tmp_path, width):
    path = constant_gap(tmp_path, width=width)
    before = path.read_bytes()
    report = inspect_signal_dropouts(path)
    event, = report["events"]
    assert report["sourceArtifactSha256"] == sha256_file(path)
    assert report["durationSeconds"] == 8
    assert report["check"]["status"] == "needs_review"
    assert event["check"] == "mix_dropout_suspected"
    assert event["category"] == "mix_dropout"
    assert event["kind"] == "technicalSignalObservation"
    assert event["startSeconds"] == 2.7
    assert event["endSeconds"] == 3.2
    assert event["observed"]["gapDurationSeconds"] == 0.5
    assert event["observed"]["relativeDepthDb"] >= 45
    assert event["observed"]["beforeBoundaryJump"] > 0.19
    assert event["observed"]["afterBoundaryJump"] > 0.19
    assert event["observed"]["intent"] == "unknown"
    assert event["retryEligible"] is False
    assert path.read_bytes() == before


def test_gap_across_one_second_chunk_boundaries_is_one_complete_observation(tmp_path):
    path = constant_gap(tmp_path, start=2.8, end=4.2)
    event, = inspect_signal_dropouts(path)["events"]
    assert event["startSeconds"] == 2.8
    assert event["endSeconds"] == 4.2
    assert event["observed"]["gapDurationSeconds"] == 1.4


@pytest.mark.parametrize("gap_seconds", [0.08, 4.0])
def test_minimum_and_maximum_gap_durations_are_inclusive(tmp_path, gap_seconds):
    event, = inspect_signal_dropouts(constant_gap(tmp_path, start=2, end=2 + gap_seconds))["events"]
    assert event["observed"]["gapDurationSeconds"] == gap_seconds


def test_near_zero_one_lsb_dither_in_16_bit_gap_is_measured(tmp_path):
    samples = np.full(8 * RATE, 0.2)
    samples[2 * RATE:3 * RATE] = np.tile([1 / 32768, -1 / 32768], RATE // 2)
    path = write_audio(tmp_path / "dither.wav", samples)
    event, = inspect_signal_dropouts(path)["events"]
    assert event["observed"]["duringRmsDbfs"] < -90
    assert event["observed"]["gapPeak"] == pytest.approx(1 / 32768)


@pytest.mark.parametrize("start, end", [(0, 0.5), (0.5, 0.8), (7.5, 8), (7.2, 7.5), (2, 2.04), (2, 6.01)])
def test_intro_outro_short_crossings_and_overlong_silence_are_excluded(tmp_path, start, end):
    assert inspect_signal_dropouts(constant_gap(tmp_path, start=start, end=end))["events"] == []


def test_sine_sample_zero_crossings_do_not_become_dropouts(tmp_path):
    samples = 0.2 * np.sin(2 * np.pi * 220 * np.arange(8 * RATE) / RATE)
    path = write_audio(tmp_path / "tone.wav", samples)
    assert inspect_signal_dropouts(path)["events"] == []


def test_smooth_fade_into_and_out_of_silence_is_excluded(tmp_path):
    samples = np.full(8 * RATE, 0.2)
    ramp = RATE // 4
    samples[2 * RATE - ramp:2 * RATE] *= np.linspace(1, 0, ramp)
    samples[2 * RATE:3 * RATE] = 0
    samples[3 * RATE:3 * RATE + ramp] *= np.linspace(0, 1, ramp)
    report = inspect_signal_dropouts(write_audio(tmp_path / "fade.wav", samples))
    assert report["events"] == []
    assert report["check"]["observed"]["rejectedSmoothBoundaryCount"] == 1


def test_quiet_signal_does_not_supply_audible_active_flanks(tmp_path):
    samples = np.full(8 * RATE, 0.0005)
    samples[2 * RATE:3 * RATE] = 0
    report = inspect_signal_dropouts(write_audio(tmp_path / "quiet.wav", samples))
    assert report["events"] == []
    assert report["check"]["status"] == "unknown"
    assert report["check"]["observed"]["rejectedQuietFlankCount"] == 1


def test_nonzero_gain_attenuation_is_outside_the_precise_digital_zero_scope(tmp_path):
    samples = np.full(8 * RATE, 0.2)
    samples[2 * RATE:3 * RATE] *= 10 ** (-45 / 20)
    assert inspect_signal_dropouts(write_audio(tmp_path / "attenuated.wav", samples))["events"] == []


def test_stereo_requires_all_channels_silent_and_avoids_phase_cancellation(tmp_path):
    samples = np.full((8 * RATE, 2), 0.2)
    samples[:, 1] *= -1
    samples[2 * RATE:3 * RATE, 0] = 0
    assert inspect_signal_dropouts(write_audio(tmp_path / "one-side.wav", samples))["events"] == []
    samples[2 * RATE:3 * RATE] = 0
    event, = inspect_signal_dropouts(write_audio(tmp_path / "both.wav", samples))["events"]
    assert event["observed"]["beforeRmsDbfs"] > -15


def test_only_fully_covering_explicit_full_rest_downgrades_a_gap(tmp_path):
    path = constant_gap(tmp_path)
    expected = [{"startSeconds": 2.7, "endSeconds": 3.2, "expectedRest": True}]
    report = inspect_signal_dropouts(path, expected_sections=expected)
    event, = report["events"]
    assert event["severity"] == "info"
    assert event["observed"]["intentAssessment"] == "consistent_with_declared_silence"
    assert event["observed"]["intent"] == "unknown"
    assert report["check"]["status"] == "observed"
    for plan in ([{"startSeconds": 2.8, "endSeconds": 3.2, "expectedRest": True}],
                 [{"startSeconds": 2.7, "endSeconds": 3.2, "accompanimentExpected": False}],
                 [{"startSeconds": 0, "endSeconds": 8, "timingIntent": "rubato"}],
                 [{"startSeconds": 0, "endSeconds": 8, "name": "intentional rest"}]):
        event, = inspect_signal_dropouts(path, expected_sections=plan)["events"]
        assert event["severity"] == "warning"


def test_signal_detector_preserves_more_than_64_events_before_combining(tmp_path):
    samples = np.full(150 * RATE, 0.2)
    for index in range(70):
        first = round((2 + index * 2) * RATE)
        samples[first:first + round(0.1 * RATE)] = 0
    report = inspect_signal_dropouts(write_audio(tmp_path / "many.wav", samples))
    assert report["totalEventCount"] == len(report["events"]) == 70
    assert report["check"]["observed"]["allEventsPreserved"] is True


def test_every_pcm_read_is_at_most_one_second(tmp_path, monkeypatch):
    path = constant_gap(tmp_path, channels=2)
    original = wave.Wave_read.readframes
    counts = []

    def tracked(audio, frames):
        counts.append(frames)
        return original(audio, frames)

    monkeypatch.setattr(wave.Wave_read, "readframes", tracked)
    inspect_signal_dropouts(path)
    assert counts and max(counts) <= RATE
    assert RATE // 2 in counts


def test_source_change_during_inspection_is_rejected(tmp_path, monkeypatch):
    path = constant_gap(tmp_path)
    original = signal_diagnostics._read
    changed = False

    def mutate(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            with path.open("ab") as stream:
                stream.write(b"changed")
            changed = True
        return result

    monkeypatch.setattr(signal_diagnostics, "_read", mutate)
    with pytest.raises(ValueError, match="changed"):
        inspect_signal_dropouts(path)


def test_invalid_ranges_truncated_pcm_and_overlong_audio_are_rejected(tmp_path):
    path = constant_gap(tmp_path)
    with pytest.raises(ValueError, match="range"):
        inspect_signal_dropouts(path, expected_sections=[{"startSeconds": True, "endSeconds": 3}])
    path.write_bytes(path.read_bytes()[:-4])
    with pytest.raises(ValueError, match="truncated"):
        inspect_signal_dropouts(path)
    long_path = write_audio(tmp_path / "overlong.wav", np.full(611, 0.2), rate=1)
    with pytest.raises(ValueError, match="610"):
        inspect_signal_dropouts(long_path)
