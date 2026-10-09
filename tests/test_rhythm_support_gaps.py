"""Explicitly unsupported windows stay unavailable without hiding distant evidence."""

from copy import deepcopy
import json
import wave

import numpy as np
import pytest

from local_music_engine.rhythm_diagnostics import MAX_POINTS, _series, diagnose_rhythm
from local_music_engine.rhythm_evidence import load_cached_stem_evidence
from local_music_engine.storage import sha256_file
from test_rhythm_diagnostics import only, stems


def omit_window(data, start, end, *, source_ranges=False):
    data = deepcopy(data)
    data["frameSeries"]["points"] = [row for row in data["frameSeries"]["points"] if not start <= row[0] < end]
    ranges = [[0, start], [end, 40]]
    if source_ranges:
        data["source"]["validatedRanges"] = ranges
    else:
        data["validatedRanges"] = ranges
    return data


@pytest.mark.parametrize("source_ranges", [False, True])
def test_excluded_aligned_hole_does_not_discard_a_later_supported_drop(source_ranges):
    backing, vocal = stems(step=0.25)
    backing = omit_window(backing, 6, 6.25, source_ranges=source_ranges)
    vocal = omit_window(vocal, 6, 6.25, source_ranges=source_ranges)
    measured = _series(backing, 40)
    assert measured is not None
    times, rms, mix, step = measured
    assert step == 0.25
    assert len(times) == 160
    assert np.isnan(rms[times == 6.125]).all()
    assert mix is not None and np.isnan(mix[times == 6.125]).all()
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    event, = only(report, "backing_dropout")
    assert event["startSeconds"] == 16
    assert event["endSeconds"] == 18


@pytest.mark.parametrize("component,start,end", [("vocal", 16.5, 16.75), ("backing", 16.5, 16.75),
                                                    ("backing", 15.5, 15.75), ("vocal", 15.5, 15.75)])
def test_missing_vocal_or_backing_support_cannot_bridge_the_dip_or_its_flanks(component, start, end):
    backing, vocal = stems(step=0.25)
    if component == "backing":
        backing = omit_window(backing, start, end)
    else:
        vocal = omit_window(vocal, start, end)
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    assert not only(report, "backing_dropout")


@pytest.mark.parametrize("range_mode", ["absent", "full", "partial"])
def test_unaccounted_missing_window_is_rejected_even_with_other_explicit_support(range_mode):
    backing, _ = stems(step=0.25)
    backing = omit_window(backing, 6, 6.5)
    if range_mode == "absent":
        backing.pop("validatedRanges")
    elif range_mode == "full":
        backing["validatedRanges"] = [[0, 40]]
    else:
        backing["validatedRanges"] = [[0, 6], [6.25, 40]]
    assert _series(backing, 40) is None


def test_non_aligned_timestamp_hole_is_not_regularized():
    backing, _ = stems(step=0.25)
    backing = omit_window(backing, 6, 6.25)
    for row in backing["frameSeries"]["points"]:
        if row[0] >= 6.25:
            row[0] += 0.02
    backing["frameSeries"]["points"] = [row for row in backing["frameSeries"]["points"] if row[0] <= 40]
    assert _series(backing, 40) is None


def test_validity_hole_between_vocal_frame_centers_cannot_be_interpolated_into_a_supported_flank():
    backing, _ = stems(step=0.1)
    _, vocal = stems(step=0.5)
    vocal["validatedRanges"] = [[0, 15.6], [15.7, 40]]
    assert all(not 15.6 <= row[0] < 15.7 for row in vocal["frameSeries"]["points"])
    measured = _series(vocal, 40)
    assert measured is not None
    times, rms, _, _ = measured
    assert np.isnan(rms[times == 15.75]).all()
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    assert not only(report, "backing_dropout")


def test_adjacent_supported_ranges_do_not_create_an_artificial_validity_gap():
    backing, vocal = stems(step=0.25)
    for data in (backing, vocal):
        data["validatedRanges"] = [[0, 16.1], [16.1, 40]]
    report = diagnose_rhythm(duration_seconds=40, accompaniment=backing, vocal=vocal)
    assert len(only(report, "backing_dropout")) == 1


@pytest.mark.parametrize("mutation", ["coarse", "unordered"])
def test_explicit_support_does_not_relax_coarse_or_unordered_input_rejection(mutation):
    backing, _ = stems(step=0.25)
    backing["validatedRanges"] = [[0, 40]]
    if mutation == "coarse":
        backing["frameSeries"]["points"] = backing["frameSeries"]["points"][::4]
    else:
        backing["frameSeries"]["points"][30][0] = 0
    assert _series(backing, 40) is None


def test_regularized_point_count_remains_bounded():
    times = np.r_[0.0005 + np.arange(11) * 0.001, 609.9995]
    data = {"source": {}, "windowSeconds": 0.001,
            "validatedRanges": [[0, 0.011], [609.999, 610]],
            "frameSeries": {"columns": ["timeSeconds", "rmsDbfs"],
                            "points": np.column_stack((times, np.full(len(times), -20))).tolist()}}
    assert (times[-1] - times[0]) / 0.001 > MAX_POINTS
    assert _series(data, 610) is None


@pytest.mark.parametrize("duration", [40, 40.1])
def test_native_cache_with_an_interior_changed_pcm_window_retains_a_distant_measured_drop(tmp_path, duration):
    rate, window_frames = 44100, 11025
    time = np.arange(round(duration * rate)) / rate
    vocal = 0.14 * np.sin(2 * np.pi * 220 * time)
    backing = np.where((time >= 16) & (time < 18), 0.003, 0.1) * np.sin(2 * np.pi * 440 * time)
    original_pcm = np.rint((vocal + backing) * 32768).astype("<i2")
    changed_pcm = original_pcm.copy()
    changed_pcm[6 * rate:round(6.25 * rate)] *= -1
    source, target = tmp_path / "source.wav", tmp_path / "changed-window.wav"
    for path, pcm in ((source, original_pcm), (target, changed_pcm)):
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(pcm.tobytes())
    mix = original_pcm.astype(np.float32) / 32768
    rms = lambda samples: float(np.sqrt(np.mean(samples * samples)))
    records = [[start / rate, min(duration, (start + window_frames) / rate),
                rms(mix[start:start + window_frames]), rms(vocal[start:start + window_frames]),
                rms(backing[start:start + window_frames])]
               for start in range(0, len(mix), window_frames)]
    cache = tmp_path / "source-stems.json"
    cache.write_text(json.dumps({"model": "known synthetic components",
        "frameEvidence": {"sourceArtifactSha256": sha256_file(source), "windowSeconds": 0.25,
            "frameSeries": {"columns": ["startSeconds", "endSeconds", "mixRms", "vocalRms", "accompanimentRms"],
                            "points": records}}}), encoding="utf-8")
    evidence = load_cached_stem_evidence(cache, audio=target, evidence_audio=source)
    assert evidence["provenance"]["validPcmRanges"] == [
        {"startSeconds": 0, "endSeconds": 6}, {"startSeconds": 6.25, "endSeconds": duration}]
    assert evidence["accompaniment"]["validatedRanges"] == evidence["provenance"]["validPcmRanges"]
    assert evidence["vocal"]["validatedRanges"] == evidence["provenance"]["validPcmRanges"]
    report = diagnose_rhythm(duration_seconds=duration, accompaniment=evidence["accompaniment"], vocal=evidence["vocal"])
    event, = only(report, "backing_dropout")
    assert event["startSeconds"] == 16
    assert event["endSeconds"] == 18
    assert event["observed"]["relativeDepthDb"] > 30
    assert report["checks"]["backingContinuity"]["status"] == "needs_review"
