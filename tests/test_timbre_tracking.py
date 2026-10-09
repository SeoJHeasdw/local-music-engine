"""Authored hit schedules exercise repetition-referenced timing without any model.

Every hit time is written by the test, so the displacement the tracker should
measure is known exactly. Hits are moved in the schedule, never by resampling
audio: their timbre is unchanged and only their timing differs.
The fixtures are synthetic voices, not semantically isolated real instruments.
"""

import json
import wave

import numpy as np
import pytest

from local_music_engine.music_structure import MusicStructureError
from local_music_engine.storage import sha256_file
from local_music_engine.timbre_tracking import EXPECTED_TIMING_KIND, HIT_COLUMNS, VERSION, analyze_timbre_timing


RATE = 8000
BAR = 2.0
BARS = 20
DURATION = BARS * BAR


def voices():
    rng = np.random.default_rng(541)
    time = np.arange(round(0.12 * RATE)) / RATE
    long = np.arange(round(0.4 * RATE)) / RATE
    return {
        "kick": (0.5 * np.sin(2 * np.pi * (90 - 200 * time) * time) + 0.03 * rng.normal(size=len(time))) * np.exp(-time * 45),
        "snare": (0.22 * rng.normal(size=len(time)) + 0.15 * np.sin(2 * np.pi * 210 * time)) * np.exp(-time * 60),
        "hat": 0.12 * np.diff(rng.normal(size=len(time) + 1)) * np.exp(-time * 180),
        "bass": 0.25 * np.sin(2 * np.pi * 55 * long) * np.minimum(1, long * RATE / 40) * np.exp(-long * 6),
    }


def pattern(*, variation_every=None):
    """A two-second bar repeated twenty times: (authored time, voice).

    Each hit carries a fixed sub-2 ms offset, as played or generated music
    does; without it every repetition would be sample-identical.
    """
    events = []
    for bar in range(BARS):
        start = bar * BAR
        kicks = [0.0, 1.0, 1.75]
        if variation_every and bar % variation_every == variation_every - 1:
            kicks = [0.0, 0.75, 1.0, 1.25]
        events += [(start + kick, "kick") for kick in kicks]
        events += [(start + snare, "snare") for snare in (0.5, 1.5)]
        events += [(start + hat * 0.25, "hat") for hat in range(8)]
        events += [(start + bass, "bass") for bass in (0.0, 1.0)]
    offsets = np.random.default_rng(2026).uniform(-0.0015, 0.0015, len(events))
    return [(time + float(offset), voice) for (time, voice), offset in zip(events, offsets)]


def render(events, *, duration=DURATION, gain=1.0):
    shapes = voices()
    samples = np.zeros(round(duration * RATE))
    for time, voice in events:
        first = round(time * RATE)
        if 0 <= first < len(samples):
            shape = shapes[voice][:len(samples) - first]
            samples[first:first + len(shape)] += shape
    time = np.arange(len(samples)) / RATE
    # A sustained chord: repetition evidence must come from attacks, not from tone.
    samples += 0.06 * np.sin(2 * np.pi * 220 * time) + 0.05 * np.sin(2 * np.pi * 277 * time)
    return samples * gain


def write_audio(path, samples):
    samples = np.asarray(samples)
    pcm = np.clip(np.rint(samples * 32768), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1 if samples.ndim == 1 else samples.shape[1])
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(pcm.tobytes())
    return path


def triangle(time, start, end, amplitude, period=0.7):
    """Alternating early/late displacement; its period is unrelated to the bar."""
    if not start <= time <= end:
        return 0.0
    phase = ((time - start) / period) % 1.0
    return amplitude * (4 * phase if phase < 0.25 else 2 - 4 * phase if phase < 0.75 else 4 * phase - 4)


def bump(time, start, end, amplitude):
    """One gradual slowing and recovery, returning to the original phase."""
    return amplitude * np.sin(np.pi * (time - start) / (end - start)) ** 2 if start <= time <= end else 0.0


def displaced(shift):
    return [(time + shift(time), voice) for time, voice in pattern()]


def analyze(tmp_path, name, events, **render_options):
    return analyze_timbre_timing(write_audio(tmp_path / f"{name}.wav", render(events, **render_options)))


def warnings(report):
    return [event for event in report["events"] if event["severity"] == "warning"]


def hits(report):
    return np.asarray(report["hits"]["points"], dtype=float).reshape(-1, len(HIT_COLUMNS))


def authored_displacements(report, shift, start, end):
    """Pair measured hits with the authored hit that was rendered at that time."""
    authored = np.array(sorted({time for time, _ in pattern()}))
    rendered = authored + np.array([shift(time) for time in authored])
    rows = hits(report)
    rows = rows[(rows[:, 0] > start) & (rows[:, 0] < end)]
    nearest = np.argmin(np.abs(rows[:, 0][:, None] - rendered[None, :]), axis=1)
    return rows, (rendered - authored)[nearest]


def test_steady_repeating_rhythm_is_measured_on_time_with_full_support(tmp_path):
    report = analyze(tmp_path, "steady", pattern())
    assert not report["events"]
    assert report["check"]["status"] == "observed"
    assert report["supportedFraction"] >= 0.9
    assert report["linkedHitCount"] >= 0.9 * report["measuredAttackCount"]
    assert np.max(np.abs(hits(report)[:, 2])) < 0.005
    authored = np.array(sorted({time for time, _ in pattern()}))
    measured = np.asarray(report["measuredOnsetTimesSeconds"])
    # Measured hits are real attacks: nothing is placed on an invented grid.
    assert np.mean(np.min(np.abs(measured[:, None] - authored[None, :]), axis=1) <= 0.03) >= 0.95


@pytest.mark.parametrize("milliseconds, period", [(30, 0.7), (60, 0.7), (120, 0.9)])
def test_sustained_alternating_displacement_is_measured_and_reported(tmp_path, milliseconds, period):
    shift = lambda time: triangle(time, 16, 22, milliseconds / 1000, period)
    report = analyze(tmp_path, "wobble", displaced(shift))
    found = warnings(report)
    assert len(found) == 1
    event = found[0]
    assert event["check"] == "repeated_hit_timing_shift_suspected" and event["category"] == "beat_timing"
    assert event["retryEligible"] is False
    overlap = min(22, event["endSeconds"]) - max(16, event["startSeconds"])
    assert overlap / 6 >= 0.8 and overlap / (event["endSeconds"] - event["startSeconds"]) >= 0.8
    rows, truth = authored_displacements(report, shift, 16, 22)
    assert len(rows) >= 15
    # Measured displacement follows the authored schedule hit by hit.
    assert np.mean(np.abs(rows[:, 3] - truth) <= 0.015) >= 0.8
    assert np.median(np.abs(rows[:, 3] - truth)) <= 0.005
    assert report["check"]["status"] == "needs_review"


def test_two_second_burst_between_stable_passages_is_reported(tmp_path):
    report = analyze(tmp_path, "burst", displaced(lambda time: triangle(time, 18, 20.1, 0.06)))
    found = warnings(report)
    assert len(found) == 1
    assert 17.5 <= found[0]["startSeconds"] <= 18.6 and 19.5 <= found[0]["endSeconds"] <= 20.6


def test_displacement_below_the_measurable_floor_is_not_reported(tmp_path):
    shift = lambda time: triangle(time, 16, 22, 0.012)
    report = analyze(tmp_path, "tiny", displaced(shift))
    assert not warnings(report)
    rows, truth = authored_displacements(report, shift, 16, 22)
    assert np.median(np.abs(rows[:, 3] - truth)) <= 0.005


def test_gradual_tempo_change_is_followed_by_the_expected_curve_not_warned(tmp_path):
    shift = lambda time: bump(time, 15, 23, 0.2)
    report = analyze(tmp_path, "smooth", displaced(shift))
    assert not warnings(report)
    smooth = [event for event in report["events"] if event["check"] == "smooth_timing_change_observed"]
    assert len(smooth) == 1 and smooth[0]["severity"] == "info" and smooth[0]["observed"]["mode"] == "tempo_drift"
    assert 150 <= smooth[0]["observed"]["peakExpectedDisplacementMilliseconds"] <= 230
    rows, truth = authored_displacements(report, shift, 15, 23)
    # The change is supported and measured, not silently skipped.
    assert len(rows) >= 20
    assert np.mean(np.abs(rows[:, 3] - truth) <= 0.02) >= 0.8
    assert np.max(np.abs(rows[:, 2])) < 0.015


def test_recurring_variation_and_single_changed_bar_are_not_timing_disturbances(tmp_path):
    assert not warnings(analyze(tmp_path, "recurring", pattern(variation_every=4)))
    fill = [(time + (0.12 if voice == "kick" and 20.0 <= time < 22.0 else 0.0), voice) for time, voice in pattern()]
    assert not warnings(analyze(tmp_path, "fill", fill))


def test_freely_timed_voice_over_a_steady_rhythm_is_not_a_timing_disturbance(tmp_path):
    rng = np.random.default_rng(7)
    free = [(float(time), "snare") for time in np.sort(rng.uniform(1, DURATION - 1, 60))]
    report = analyze(tmp_path, "free", pattern() + free)
    assert not warnings(report)
    assert report["check"]["status"] == "observed"


def test_music_without_repetition_abstains_instead_of_approving(tmp_path):
    rng = np.random.default_rng(11)
    events = [(float(time), str(voice)) for time, voice in
              zip(np.sort(rng.uniform(0.5, DURATION - 0.5, 220)), rng.choice(["kick", "snare", "hat"], 220))]
    report = analyze(tmp_path, "random", events)
    assert not report["events"]
    assert report["check"]["status"] == "unknown"
    assert report["supportedFraction"] == 0 and report["supportedRangesSeconds"] == []


@pytest.mark.parametrize("kind", ["silence", "tone", "short"])
def test_absent_or_insufficient_attack_evidence_cannot_raise_timing_warning(tmp_path, kind):
    if kind == "silence":
        samples = np.zeros(12 * RATE)
    elif kind == "tone":
        samples = 0.25 * np.sin(2 * np.pi * 220 * np.arange(12 * RATE) / RATE)
    else:
        samples = render(pattern(), duration=3)
    report = analyze_timbre_timing(write_audio(tmp_path / f"{kind}.wav", samples))
    assert not report["events"]
    assert report["check"]["status"] == "unknown"


def test_level_and_antiphase_stereo_do_not_change_the_measurement(tmp_path):
    events = displaced(lambda time: triangle(time, 16, 22, 0.06))
    reference = analyze(tmp_path, "reference", events)
    quiet = analyze(tmp_path, "quiet", events, gain=0.4)
    samples = render(events)
    stereo = analyze_timbre_timing(write_audio(tmp_path / "stereo.wav", np.column_stack((samples, -samples))))
    assert stereo["measuredOnsetTimesSeconds"] == reference["measuredOnsetTimesSeconds"]
    assert stereo["events"] == reference["events"] and stereo["hits"] == reference["hits"]
    # 16-bit rounding at another level may move a peak by one 10 ms frame or one
    # reference's vote, never the measured displacement.
    assert np.allclose(quiet["measuredOnsetTimesSeconds"], reference["measuredOnsetTimesSeconds"], atol=0.0101)
    assert [(event["startSeconds"], event["endSeconds"]) for event in quiet["events"]] == \
        [(event["startSeconds"], event["endSeconds"]) for event in reference["events"]]
    assert np.allclose(hits(quiet)[:, 2:4], hits(reference)[:, 2:4], atol=0.002)
    assert np.max(np.abs(hits(quiet)[:, 4:] - hits(reference)[:, 4:])) <= 2


def test_measured_expected_and_model_times_stay_separate_and_source_bound(tmp_path):
    path = write_audio(tmp_path / "source.wav", render(displaced(lambda time: triangle(time, 16, 22, 0.06))))
    before = path.read_bytes()
    report = analyze_timbre_timing(path)
    assert report["version"] == VERSION == "timbre-timing-v2"
    assert report["sourceArtifactSha256"] == sha256_file(path)
    assert report["sourceAudioBytes"] == len(before) and report["durationSeconds"] == DURATION
    assert report["modelInference"] is False and report["modelPredictedBeatTimesSeconds"] == []
    assert report["expectedTimingKind"] == EXPECTED_TIMING_KIND
    assert path.read_bytes() == before
    rows = hits(report)
    measured = set(report["measuredOnsetTimesSeconds"])
    # Every reported hit is a measured attack; the expected time is derived from it.
    assert all(time in measured for time in rows[:, 0])
    assert np.allclose(rows[:, 0] - rows[:, 1], rows[:, 2], atol=0.0002)
    assert np.all(np.diff(report["measuredOnsetTimesSeconds"]) > 0)
    event = warnings(report)[0]
    assert event["observed"]["expectedTimingKind"] == EXPECTED_TIMING_KIND
    assert len(event["observed"]["measuredOnsetTimesSeconds"]) == len(event["observed"]["expectedTimesSeconds"])
    assert event["observed"]["measuredOnsetTimesSeconds"] != event["observed"]["expectedTimesSeconds"]
    json.dumps(report, allow_nan=False)


def test_truncated_pcm_rejects_incomplete_timbre_evidence(tmp_path):
    path = write_audio(tmp_path / "truncated.wav", render(pattern(), duration=3))
    path.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(MusicStructureError, match="truncated"):
        analyze_timbre_timing(path)
