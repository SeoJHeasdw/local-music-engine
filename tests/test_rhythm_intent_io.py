"""Explicit intent stays source-bound, immutable and separate from human review."""

from copy import deepcopy
import json
from pathlib import Path
import wave

import pytest

from local_music_engine import rhythm_inspection
from local_music_engine.cli import build_parser, run
from local_music_engine.rhythm_diagnostics import VERSION as DIAGNOSTICS_VERSION
from local_music_engine.rhythm_intent import MAX_FILE_BYTES, load_rhythm_intent
from local_music_engine.storage import sha256_file
from test_rhythm_inspection import saved_song


def wav(tmp_path):
    path = tmp_path / "source.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 8000 * 3)
    return path


def intent_file(tmp_path, audio, *, sections=None):
    path = tmp_path / "intent.json"
    path.write_text(json.dumps({"version": "rhythm-intent-v1", "sourceArtifactSha256": sha256_file(audio),
        "sections": sections if sections is not None else [{"startSeconds": 1, "endSeconds": 2,
            "name": "planned break", "expectedRest": True}]}), encoding="utf-8")
    return path


def candidate_audio(store, candidate_id):
    project = store.load()
    candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
    return store.resolve_artifact(artifact)


def test_intent_loader_normalizes_and_freezes_the_loaded_bytes(tmp_path):
    audio = wav(tmp_path)
    before = audio.read_bytes()
    path = intent_file(tmp_path, audio, sections=[
        {"startSeconds": 2, "endSeconds": 3, "timingIntent": "tempo_change"},
        {"startSeconds": 0, "endSeconds": 1, "accompanimentExpected": False},
    ])
    initial_hash = sha256_file(path)
    snapshot = load_rhythm_intent(path, audio=audio)
    path.write_text("{}", encoding="utf-8")
    assert snapshot.intent_file_sha256 == initial_hash
    assert snapshot.sections[0]["startSeconds"] == 0.0
    with pytest.raises(TypeError):
        snapshot.sections[0]["endSeconds"] = 3
    copy = snapshot.to_dict()
    copy["sections"][0]["endSeconds"] = 3
    assert snapshot.sections[0]["endSeconds"] == 1
    assert audio.read_bytes() == before


@pytest.mark.parametrize("section", [
    {"startSeconds": True, "endSeconds": 2, "expectedRest": True},
    {"startSeconds": 1, "endSeconds": False, "expectedRest": True},
    {"startSeconds": -1, "endSeconds": 2, "expectedRest": True},
    {"startSeconds": 1, "endSeconds": 4, "expectedRest": True},
    {"startSeconds": 2, "endSeconds": 1, "expectedRest": True},
    {"startSeconds": 1, "endSeconds": 2, "expectedRest": "yes"},
    {"startSeconds": 1, "endSeconds": 2, "accompanimentExpected": 0},
    {"startSeconds": 1, "endSeconds": 2, "timingIntent": "groove"},
    {"startSeconds": 1, "endSeconds": 2, "name": "x" * 241, "expectedRest": True},
    {"startSeconds": 1, "endSeconds": 2, "expectedRest": True, "confirmModelError": True},
    {"startSeconds": float("inf"), "endSeconds": 2, "expectedRest": True},
])
def test_malformed_or_out_of_audio_sections_are_rejected(tmp_path, section):
    audio = wav(tmp_path)
    path = intent_file(tmp_path, audio, sections=[section])
    before = audio.read_bytes()
    with pytest.raises(ValueError):
        load_rhythm_intent(path, audio=audio)
    assert audio.read_bytes() == before


@pytest.mark.parametrize("change", [
    {"version": "rhythm-intent-v2"}, {"sourceArtifactSha256": "0" * 64},
    {"sourceArtifactSha256": True}, {"sections": None}, {"sections": "rubato"},
    {"sections": [{"startSeconds": 0, "endSeconds": 1, "expectedRest": True}] * 129},
    {"humanReview": "approved"},
])
def test_invalid_file_contract_is_rejected(tmp_path, change):
    audio = wav(tmp_path)
    path = intent_file(tmp_path, audio)
    supplied = json.loads(path.read_text())
    supplied.update(change)
    path.write_text(json.dumps(supplied), encoding="utf-8")
    with pytest.raises(ValueError):
        load_rhythm_intent(path, audio=audio)


def test_overlarge_duplicate_key_and_nonfinite_json_are_rejected(tmp_path):
    audio = wav(tmp_path)
    path = tmp_path / "intent.json"
    for content in (b" " * (MAX_FILE_BYTES + 1), b'{"version":"rhythm-intent-v1","version":"rhythm-intent-v1"}',
                    b'{"sections":[{"startSeconds":NaN}]}', b"\xff"):
        path.write_bytes(content)
        with pytest.raises(ValueError):
            load_rhythm_intent(path, audio=audio)


def test_cli_intent_alone_runs_diagnostics_and_retains_input_provenance(tmp_path):
    audio = wav(tmp_path)
    path = intent_file(tmp_path, audio)
    before = audio.read_bytes()
    report = run(build_parser().parse_args(["analyze-rhythm", str(audio), "--intent-json", str(path)]))
    assert report["diagnostics"]["version"] == DIAGNOSTICS_VERSION
    assert report["diagnostics"]["expectedSections"][0]["expectedRest"] is True
    assert report["intentSnapshot"]["sourceArtifactSha256"] == sha256_file(audio)
    assert report["intentFileSha256"] == sha256_file(path)
    assert audio.read_bytes() == before


def test_direct_expected_sections_fail_before_dsp(tmp_path, monkeypatch):
    audio = wav(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid intent must be rejected before audio analysis")

    monkeypatch.setattr(rhythm_inspection, "analyze_music_structure", unexpected)
    with pytest.raises(ValueError, match="range"):
        rhythm_inspection.inspect_audio_rhythm(audio, expected_sections=[
            {"startSeconds": 0, "endSeconds": 4, "expectedRest": True}])


@pytest.mark.parametrize("change", [
    {"sourceArtifactSha256": "0" * 64},
    {"sections": [{"startSeconds": 0, "endSeconds": 11, "expectedRest": True}]},
])
def test_saved_inspection_rejects_bad_intent_before_any_project_mutation(tmp_path, change):
    store, candidate_id = saved_song(tmp_path)
    audio = candidate_audio(store, candidate_id)
    path = intent_file(tmp_path, audio)
    supplied = json.loads(path.read_text())
    supplied.update(change)
    path.write_text(json.dumps(supplied), encoding="utf-8")
    before = deepcopy(store.load())
    manifest_bytes = (store.root / "project.json").read_bytes()
    with pytest.raises(ValueError):
        run(build_parser().parse_args(["inspect-rhythm", str(store.root), "--candidate-id", candidate_id,
                                      "--intent-json", str(path)]))
    assert store.load() == before
    assert (store.root / "project.json").read_bytes() == manifest_bytes


def test_saved_report_and_job_keep_original_snapshot_when_file_changes(tmp_path, monkeypatch):
    store, candidate_id = saved_song(tmp_path)
    audio = candidate_audio(store, candidate_id)
    path = intent_file(tmp_path, audio)
    initial_hash = sha256_file(path)
    original = deepcopy(store.load())
    original_candidate = store.find_by_id(original, "candidates", "candidateId", candidate_id)
    original_inspect = rhythm_inspection.inspect_audio_rhythm

    def changed_file(*args, **kwargs):
        path.write_text("{}", encoding="utf-8")
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(rhythm_inspection, "inspect_audio_rhythm", changed_file)
    report = rhythm_inspection.inspect_saved_candidate(store.root, candidate_id=candidate_id, intent_path=path)
    assert report["intentFileSha256"] == initial_hash != sha256_file(path)
    assert report["intentSnapshot"]["sections"][0]["name"] == "planned break"
    assert report["rhythm"]["intentSnapshot"] == report["intentSnapshot"]
    saved_report = json.loads(Path(report["reportPath"]).read_text(encoding="utf-8"))
    assert saved_report["intentSnapshot"] == report["intentSnapshot"]
    project = store.load()
    job = project["jobs"][-1]
    assert job["parameters"]["intentFileSha256"] == initial_hash
    assert job["parameters"]["intentSnapshot"] == report["intentSnapshot"]
    after = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    assert after["humanReview"] == original_candidate["humanReview"]
    assert after.get("quality") == original_candidate.get("quality")
    assert sha256_file(audio) == report["sourceArtifactSha256"]
