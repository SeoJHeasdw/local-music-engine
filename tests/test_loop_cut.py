"""Where a BGM loop is cut: on whole bars between downbeat kicks, or not at all with a reason."""

import wave
from pathlib import Path

import numpy as np
import pytest

from local_music_engine import loop_cut
from local_music_engine.loop_cut import LoopNotFound, choose_loop, downbeat_grid, find_loop, kick_onsets, render_loop

RATE = 48_000
CHORDS = ((220.0, 277.2, 329.6), (196.0, 246.9, 293.7), (174.6, 220.0, 261.6), (164.8, 207.7, 246.9))


def groove(layout: str, *, bpm: float = 90.0, end_bpm: float | None = None, four_on_floor: bool = False,
           swing: float = 0.0, peak: float = 0.5) -> np.ndarray:
    """One character per bar: i intro (chords), g groove (kick, snare, hats, chords), b break (chords), e ending (silence).

    The chords change every bar and repeat every four, as a generated loop-based song does.
    ``end_bpm`` makes the tempo drift linearly to it by the last bar.
    """

    bars = len(layout)
    tempi = np.linspace(bpm, end_bpm or bpm, bars)
    starts = np.concatenate([[0.0], np.cumsum(4 * 60 / tempi)])
    audio = np.zeros(int(starts[-1] * RATE) + RATE)
    noise = np.random.default_rng(3)
    snare = np.diff(noise.standard_normal(int(0.12 * RATE)), prepend=0) * np.exp(-np.arange(int(0.12 * RATE)) / RATE * 30)
    hat = np.diff(noise.standard_normal(int(0.03 * RATE)), prepend=0) * np.exp(-np.arange(int(0.03 * RATE)) / RATE * 150)
    for bar, kind in enumerate(layout):
        if kind == "e":
            continue
        begin, length = starts[bar], starts[bar + 1] - starts[bar]
        t = np.arange(int(length * RATE)) / RATE
        first = int(begin * RATE)
        # Played like an instrument: each bar's chord fades in and out over 10 ms.
        edge = np.minimum(1.0, np.minimum(t, t[-1] - t) / 0.01)
        chord = sum(np.sin(2 * np.pi * frequency * t) for frequency in CHORDS[bar % 4]) / 3
        audio[first:first + len(t)] += 0.15 * chord * edge
        if kind != "g":
            continue
        beat = length / 4
        for step in range(4):
            if four_on_floor or step in (0, 2):
                hit = np.arange(int(0.4 * RATE)) / RATE
                kick = np.sin(2 * np.pi * 55 * hit) * np.exp(-hit * 9)
                at = first + int(step * beat * RATE)
                audio[at:at + len(kick)] += kick[:len(audio) - at] * (1.0 if step == 0 or four_on_floor else 0.8)
            if step in (1, 3):
                at = first + int(step * beat * RATE)
                audio[at:at + len(snare)] += 0.3 * snare[:len(audio) - at]
            for half in (0, 1):
                at = first + int((step + half * (0.5 + swing)) * beat * RATE)
                audio[at:at + len(hat)] += 0.08 * hat[:len(audio) - at]
    audio = audio / np.abs(audio).max() * peak
    return np.stack([audio, 0.95 * audio], axis=1)


def write(path: Path, samples: np.ndarray, *, width: int = 2) -> Path:
    scale = (1 << (8 * width - 1)) - 1
    integers = np.rint(np.clip(samples, -1, 1) * scale).astype("<i4")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(samples.shape[1])
        output.setsampwidth(width)
        output.setframerate(RATE)
        data = integers.astype("<i2").tobytes() if width == 2 else integers.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
        output.writeframes(data)
    return path


def read(path: Path) -> np.ndarray:
    return loop_cut._read(path)


def pair(tmp_path: Path, samples: np.ndarray) -> tuple[Path, Path]:
    """The finished version (16-bit) and its listening copy (24-bit), sample for sample alike."""

    return write(tmp_path / "finished.wav", samples), write(tmp_path / "listening.wav", samples, width=3)


HEARD_LAYOUT = "iiii" + "g" * 16 + "bbbb" + "g" * 8 + "ee"


def test_the_heard_song_layout_loops_24_bars_from_the_first_drum_bar(tmp_path):
    finished, listening = pair(tmp_path, groove(HEARD_LAYOUT))
    plan = find_loop(finished, listening, reference_bpm=90, time_signature="4")
    bar = 4 * 60 / 90
    assert plan["bars"] == 24 and plan["startBar"] == 4
    # The kick drum takes a few milliseconds to reach half its height; the cut sits 30 ms before that.
    assert 4 * bar - 0.031 < plan["startSeconds"] < 4 * bar - 0.020
    assert abs(plan["endFrame"] - plan["startFrame"] - 24 * bar * RATE) <= 2
    assert abs(plan["bpm"] - 90) < 0.01 and plan["warnings"] == []
    assert plan["tempo"]["residualRmsMs"] < 1


def test_bar_length_comes_from_the_downbeats_not_the_played_tempo_estimate(tmp_path):
    samples = groove(HEARD_LAYOUT)
    onsets, heights = kick_onsets(samples.mean(axis=1), RATE)
    # The heard song's played-tempo estimate was 90.11 for a song on a 90.0002 BPM bar line.
    grid = downbeat_grid(onsets, heights, prior_bar=4 * 60 / 90.11)
    assert abs(grid["barSeconds"] - 4 * 60 / 90) < 0.0002
    assert sorted(grid["downbeatKicks"]) == [*range(4, 20), *range(24, 32)]


def test_a_tempo_that_bends_a_little_still_loops_on_its_measured_kicks(tmp_path):
    # As a heard-as-steady song did: about 0.2% slower by the end.
    finished, listening = pair(tmp_path, groove(HEARD_LAYOUT, end_bpm=89.8))
    plan = find_loop(finished, listening, reference_bpm=90, time_signature="4")
    assert plan["bars"] == 24 and plan["startBar"] == 4
    start, end = plan["tempo"]["loopEndsBpm"]
    assert abs(end / start - 1) <= loop_cut.TEMPO_MATCH and start > end


def test_swung_hats_do_not_move_the_bar_line(tmp_path):
    finished, listening = pair(tmp_path, groove(HEARD_LAYOUT, swing=0.16))
    plan = find_loop(finished, listening, reference_bpm=90, time_signature="4")
    assert plan["bars"] == 24 and plan["startBar"] == 4


def test_four_on_the_floor_still_loops_but_says_its_downbeat_is_a_guess(tmp_path):
    finished, listening = pair(tmp_path, groove("ii" + "g" * 26 + "e", bpm=120, four_on_floor=True))
    plan = find_loop(finished, listening, reference_bpm=120, time_signature="4")
    assert plan["bars"] % 8 == 0 and plan["bars"] >= 16
    assert "phase_ambiguous" in plan["warnings"]


@pytest.mark.parametrize(("layout", "kwargs", "code"), [
    ("ii" + "b" * 24 + "e", {}, "no_kick"),                       # chords and nothing on the downbeat
    ("iiii" + "g" * 12 + "ee", {}, "groove_too_short"),
    ("iiii" + "g" * 28 + "ee", {"end_bpm": 92.0}, "tempo_unsteady"),
])
def test_what_cannot_be_measured_is_reported_instead_of_a_loop(tmp_path, layout, kwargs, code):
    samples = groove(layout, **kwargs)
    if code == "no_kick":
        # Keep a beat the played-tempo search can hear, but nothing below 120 Hz.
        t = np.arange(len(samples)) / RATE
        clicks = (np.mod(t, 60 / 90) < 0.004) * np.sin(2 * np.pi * 3000 * t) * 0.3
        samples = samples + clicks[:, None]
    finished, listening = pair(tmp_path, samples)
    with pytest.raises(LoopNotFound) as missing:
        find_loop(finished, listening, reference_bpm=90, time_signature="4")
    assert missing.value.code == code and missing.value.reason == loop_cut.REASONS[code]


def test_only_four_four_and_matching_files_are_cut(tmp_path):
    finished, listening = pair(tmp_path, groove(HEARD_LAYOUT))
    with pytest.raises(LoopNotFound, match="meter_unsupported"):
        find_loop(finished, listening, reference_bpm=90, time_signature="3")
    shorter = write(tmp_path / "short.wav", groove(HEARD_LAYOUT)[:-RATE], width=3)
    with pytest.raises(LoopNotFound, match="listening_copy_mismatch"):
        find_loop(finished, shorter, reference_bpm=90, time_signature="4")
    assert "meter_assumed" in find_loop(finished, listening, reference_bpm=90, time_signature=None)["warnings"]


def test_ends_that_drift_apart_never_match_and_short_grooves_have_no_controls():
    kicks = {bar: bar * 2.0 for bar in range(34)}
    grid = {"downbeatKicks": kicks, "coefficients": [0.0, 2.0, 0.0], "firstBar": 0}
    # A song that keeps changing: bars sixteen apart differ twice as much as bars eight apart.
    ramp = np.outer(np.arange(34, dtype=float), np.ones(72))
    with pytest.raises(LoopNotFound, match="no_matching_bars"):
        choose_loop(grid, ramp)
    with pytest.raises(LoopNotFound, match="groove_too_short"):
        choose_loop({**grid, "downbeatKicks": {bar: bar * 2.0 for bar in range(10)}}, np.zeros((10, 72)))
    repeating = np.tile(np.arange(4 * 72, dtype=float).reshape(4, 72), (9, 1))[:34]
    assert choose_loop(grid, repeating)["bars"] == 32


def test_the_loop_joins_its_end_to_its_start_without_rising_above_the_song(tmp_path):
    samples = groove(HEARD_LAYOUT, peak=0.88)
    finished, listening = pair(tmp_path, samples)
    plan = find_loop(finished, listening, reference_bpm=90, time_signature="4")
    rendered = render_loop(listening, tmp_path / "loop.wav", plan, analysis=finished)
    loop = read(tmp_path / "loop.wav")
    source = read(listening)
    assert len(loop) == plan["endFrame"] - plan["startFrame"]
    assert rendered["crossfade"]["law"] == "linear" and rendered["crossfade"]["correlation"] > 0.5
    assert np.abs(loop).max() <= np.abs(source).max() + 1e-6
    # Outside the fade the loop is the listening copy itself, sample for sample.
    fade = int(loop_cut.CROSSFADE_SECONDS * RATE)
    assert np.array_equal(loop[fade:], source[plan["startFrame"] + fade:plan["endFrame"]])
    # Wrapping around continues where the song would have gone on.
    assert np.abs(loop[0] - source[plan["endFrame"]]).max() < 0.002
    assert rendered["warnings"] == [] and len(rendered["masteringGainDb"]) == 2
