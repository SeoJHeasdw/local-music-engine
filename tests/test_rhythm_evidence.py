"""Cached stem evidence is attributed to verified PCM, never filenames alone."""

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from local_music_engine.music_structure import _decode
from local_music_engine.rhythm_evidence import RhythmEvidenceError, load_cached_stem_evidence, separator_frame_inputs
from local_music_engine.storage import sha256_file
from test_music_structure import write_audio


def cached(tmp_path, *, native=False):
    audio = tmp_path / "source.wav"
    times = np.arange(6 * 44100) / 44100
    write_audio(audio, 0.2 * np.sin(2 * np.pi * 220 * times), rate=44100)
    with wave.open(str(audio), "rb") as source:
        samples = _decode(source.readframes(source.getnframes()), 2, 1).astype(np.float32)
    step = 11025
    mix = [float(np.sqrt(np.mean(samples[index:index + step] ** 2))) for index in range(0, len(samples), step)]
    if native:
        content = {"frameEvidence": {"sourceArtifactSha256": sha256_file(audio), "windowSeconds": 0.25,
            "frameSeries": {"columns": ["startSeconds", "endSeconds", "mixRms", "vocalRms", "accompanimentRms"],
                "points": [[index * 0.25, (index + 1) * 0.25, rms, rms / 2, rms / 2] for index, rms in enumerate(mix)]}}}
    else:
        content = {"path": str(audio), "duration": 6, "window": 0.25,
            "mix": mix, "vocal": [rms / 2 for rms in mix], "accomp": [rms / 2 for rms in mix]}
    cache = tmp_path / "stems.json"
    cache.write_text(json.dumps(content))
    return audio, cache, content


@pytest.mark.parametrize("native", [False, True])
def test_cache_validation_records_actual_hashes_and_historical_provenance(tmp_path, native):
    audio, cache, _ = cached(tmp_path, native=native)
    before = audio.read_bytes()
    report = load_cached_stem_evidence(cache, audio=audio)
    assert report["provenance"]["targetArtifactSha256"] == sha256_file(audio)
    assert report["provenance"]["cacheSha256"] == sha256_file(cache)
    assert report["provenance"]["legacySourceHashMissing"] is (not native)
    assert report["provenance"]["modelRunNow"] is False
    assert report["provenance"]["validPcmRanges"] == [{"startSeconds": 0, "endSeconds": 6}]
    assert len(report["vocal"]["frameSeries"]["points"]) == 24
    assert audio.read_bytes() == before


def test_splice_can_only_reuse_byte_identical_pcm_windows(tmp_path):
    reference, cache, _ = cached(tmp_path)
    with wave.open(str(reference), "rb") as audio:
        samples = _decode(audio.readframes(audio.getnframes()), 2, 1).ravel()
    samples[:44100] *= -1
    target = tmp_path / "different-prefix.wav"
    write_audio(target, samples, rate=44100)
    report = load_cached_stem_evidence(cache, audio=target, evidence_audio=reference)
    assert report["provenance"]["validPcmRanges"] == [{"startSeconds": 1, "endSeconds": 6}]
    assert min(row[0] for row in report["vocal"]["frameSeries"]["points"]) >= 1
    assert report["provenance"]["sourceArtifactSha256"] != report["provenance"]["targetArtifactSha256"]


@pytest.mark.parametrize("mutation", ["mix", "nan", "bool", "unequal", "window"])
def test_legacy_energy_or_shape_changes_cannot_become_valid_evidence(tmp_path, mutation):
    audio, cache, content = cached(tmp_path)
    if mutation == "mix":
        content["mix"][2] *= 2
    elif mutation == "nan":
        content["vocal"][2] = float("nan")
    elif mutation == "bool":
        content["accomp"][2] = True
    elif mutation == "unequal":
        content["vocal"].pop()
    else:
        content["window"] = -1
    cache.write_text(json.dumps(content))
    with pytest.raises(RhythmEvidenceError):
        load_cached_stem_evidence(cache, audio=audio)


def test_native_cache_requires_its_original_source_hash(tmp_path):
    audio, cache, content = cached(tmp_path, native=True)
    content["frameEvidence"]["sourceArtifactSha256"] = "0" * 64
    cache.write_text(json.dumps(content))
    with pytest.raises(RhythmEvidenceError, match="hash"):
        load_cached_stem_evidence(cache, audio=audio)
    assert separator_frame_inputs(content, source_sha256=sha256_file(audio)) == {}


def test_summary_only_separator_output_cannot_support_backing_classification():
    assert separator_frame_inputs({"status": "measured", "vocalActivityFraction": 0.8}, source_sha256="hash") == {}


def test_source_bound_separator_frames_adapt_without_loading_models(tmp_path):
    audio, _, content = cached(tmp_path, native=True)
    result = separator_frame_inputs(content, source_sha256=sha256_file(audio))
    assert result["accompaniment"]["source"]["kind"] == "separated_accompaniment"
    assert result["vocal"]["source"]["sourceArtifactSha256"] == sha256_file(audio)
    assert len(result["accompaniment"]["frameSeries"]["points"]) == 24
    content["frameEvidence"]["frameSeries"]["points"][0][3] = float("nan")
    assert separator_frame_inputs(content, source_sha256=sha256_file(audio)) == {}
