"""Benchmark comparison contracts use test doubles, never actual inference."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from local_music_engine.production_rules import PRESETS
from local_music_engine.storage import ProjectStore
from local_music_engine.workflow import generate_candidates, review_candidate
from test_auto_quality import Reports
from test_music3 import FakeMusic3Client
from test_workflow import RecordingAceClient


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compare_music3_quality.py"
spec = importlib.util.spec_from_file_location("music3_comparison", MODULE_PATH)
comparison = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = comparison
assert spec.loader
spec.loader.exec_module(comparison)

LYRICS = ("[Verse]\nCity lights are fading\nYour voice stays with me\nI walk through the silence\nAnd let the night breathe\n\n"
          "[Chorus]\nStay until the morning\nLet the cold wind go\nStep into the daylight\nWe can take it slow")


@pytest.fixture
def benchmark(tmp_path):
    RecordingAceClient.fail_seeds = set()
    RecordingAceClient.loaded = ("acestep-v15-turbo", "acestep-5Hz-lm-4B")
    store = ProjectStore.initialize(tmp_path / "benchmark", title="Benchmark", lyrics=LYRICS,
        style_prompt="intimate male melodic rap, mellow late-night mood", target_duration_seconds=30,
        bpm=84, key_scale="A minor", time_signature="4",
        production_rules={"version": 1, "presetId": "emotional-hiphop",
                          "ruleIds": [*PRESETS[0]["ruleIds"], "section-development", "phrase-breathing"]})
    result = generate_candidates(store.root, seeds=[41], client_factory=RecordingAceClient, engine="ace-step")
    candidate_id = result["candidateIds"][0]
    review_candidate(store.root, candidate_id, status="listened", rating=5, note="Existing benchmark review")
    FakeMusic3Client.submitted, FakeMusic3Client.payloads = [], []
    FakeMusic3Client.fail_seeds = set()
    FakeMusic3Client.engine = comparison.ENGINE
    FakeMusic3Client.loaded_model = comparison.MODEL
    FakeMusic3Client.models_initialized = True
    return store, candidate_id


def config(tmp_path, benchmark, **changes):
    store, candidate_id = benchmark
    values = dict(baseline_project=store.root, baseline_candidate=candidate_id,
                  project_dir=tmp_path / "comparison-project", output_dir=tmp_path / "comparison-output",
                  base_url=comparison.BASE_URL, timeout=30.0, recipes="native,concise")
    return argparse.Namespace(**(values | changes))


def run(args):
    return comparison.run_comparison(args, client_factory=FakeMusic3Client,
        quality_backend=Reports([comparison.sung_text(LYRICS)]), execution_mode="test-double")


def test_matched_eight_line_generation_preserves_import_and_pending_human_verdict(tmp_path, benchmark):
    source, _ = benchmark
    before = source.manifest_path.read_bytes()
    args = config(tmp_path, benchmark)
    report = run(args)
    assert report["status"] == "completed" and report["executionMode"] == "test-double"
    assert report["fixedConditions"]["sungLineCount"] == 8
    assert report["fixedConditions"]["sungText"] == comparison.sung_text(LYRICS)
    assert report["musicalMinimumVerdict"] == "pending_human_listening"
    assert report["sourceConditioning"] is False
    assert FakeMusic3Client.submitted == [41, 41]
    assert source.manifest_path.read_bytes() == before
    assert json.loads((args.output_dir / "report.json").read_text()) == report
    store = ProjectStore(args.project_dir)
    project = store.load()
    assert project["inputs"]["lyricsOriginal"] == LYRICS and project["selectedCandidateId"] is None
    imported = store.find_by_id(project, "candidates", "candidateId", report["benchmarkCandidateId"])
    assert imported["sourceImport"]["playbackOnly"] is True
    assert imported["humanReview"]["status"] == "listened" and imported["humanReview"]["rating"] == 5
    imported_request = store.find_by_id(project, "requests", "requestId", imported["requestId"])
    assert imported_request["executionKind"] == "verified-import"
    assert imported_request["originalRequest"]["parameters"]["seed"] == 41
    assert all(store.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    assert {candidate["humanReview"]["status"] for candidate in project["candidates"] if not candidate.get("sourceImport")} == {"unreviewed"}
    for row in report["recipes"]:
        assert row["generationAttempts"] == 1 and row["realGenerationAttempts"] == 0
        frozen = row["actualEngineRequests"][0]["parameters"]
        assert comparison.sung_text(frozen["lyrics"]) == comparison.sung_text(LYRICS)
        assert frozen["audio_duration"] == 30 and frozen["bpm"] == 84
        assert frozen["key_scale"] == "A minor" and frozen["seed"] == 41
        assert row["generation"]["reusedCandidateIds"] == []
        assert Path(row["export"]["externalPath"]).is_file()


def test_recipe_order_does_not_leave_native_rules_deselected(tmp_path, benchmark):
    report = run(config(tmp_path, benchmark, recipes="concise,native"))
    assert report["status"] == "completed"
    concise, native = [row["actualEngineRequests"][0]["parameters"] for row in report["recipes"]]
    assert concise["productionRules"]["selection"]["ruleIds"] == []
    assert {"melodic-hook", "expressive-performance"} <= set(native["productionRules"]["selection"]["ruleIds"])
    assert native["music3ProductionGuidance"]["preset"]["native"] is True


def test_compact_recipe_keeps_eight_lyrics_and_one_attempt_with_prompt_flow_guidance(tmp_path, benchmark):
    report = run(config(tmp_path, benchmark, recipes="compact"))
    assert report["status"] == "completed" and report["unsubmittedRecipes"] == []
    assert FakeMusic3Client.submitted == [41]
    row = report["recipes"][0]
    frozen = row["actualEngineRequests"][0]["parameters"]
    assert row["recipe"] == "compact" and row["generationAttempts"] == 1
    assert frozen["productionRules"]["selection"] == {"version": 1, "presetId": None, "ruleIds": []}
    assert frozen["audio_duration"] == 30 and frozen["seed"] == 41
    assert comparison.sung_text(frozen["lyrics"]) == comparison.sung_text(LYRICS)
    assert "lead enters promptly" in frozen["prompt"]
    assert "four short verse lines flow directly into four chorus lines" in frozen["prompt"]
    assert "sustained line endings" not in frozen["prompt"]
    assert report["musicalMinimumVerdict"] == "pending_human_listening"


@pytest.mark.parametrize("field", ["project_dir", "output_dir"])
def test_existing_directory_refused_before_contacting_music_server(tmp_path, benchmark, field):
    args = config(tmp_path, benchmark)
    path = getattr(args, field)
    path.mkdir()
    marker = path / "keep.txt"
    marker.write_text("preserve existing input")
    class NeverClient:
        def __init__(self, url):
            pytest.fail("fresh directories must be checked before server access")
    with pytest.raises(FileExistsError):
        comparison.run_comparison(args, client_factory=NeverClient)
    assert marker.read_text() == "preserve existing input"
    assert FakeMusic3Client.submitted == []


@pytest.mark.parametrize("changes", [{"timeout": float("nan")}, {"timeout": 0},
    {"timeout": float("inf")}, {"recipes": "native,native"}, {"recipes": "unknown"}])
def test_invalid_configuration_has_no_output_or_engine_side_effect(tmp_path, benchmark, changes):
    args = config(tmp_path, benchmark, **changes)
    with pytest.raises(ValueError):
        run(args)
    assert not args.output_dir.exists() and not args.project_dir.exists()
    assert FakeMusic3Client.submitted == []


def test_real_mode_rejects_test_double_before_creating_comparison(tmp_path, benchmark):
    args = config(tmp_path, benchmark)
    with pytest.raises(RuntimeError, match="actual weight-backed"):
        comparison.run_comparison(args, client_factory=FakeMusic3Client)
    assert not args.output_dir.exists() and not args.project_dir.exists()


def test_import_hash_race_is_rejected_without_registering_or_retaining_bad_copy(tmp_path, benchmark, monkeypatch):
    source, candidate_id = benchmark
    destination = ProjectStore.initialize(tmp_path / "destination", title="New", lyrics=LYRICS,
        style_prompt="hip hop", target_duration_seconds=30)
    before = destination.manifest_path.read_bytes()
    copy = comparison.shutil.copyfileobj
    def replace_before_copy(reader, writer):
        path = Path(reader.name)
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
        return copy(reader, writer)
    monkeypatch.setattr(comparison.shutil, "copyfileobj", replace_before_copy)
    with pytest.raises(ValueError, match="changed during explicit import"):
        comparison.import_benchmark(source, candidate_id, destination)
    assert destination.manifest_path.read_bytes() == before
    assert list((destination.root / "artifacts" / "benchmarks").glob("*.wav")) == []


def test_import_name_collision_never_removes_preexisting_audio(tmp_path, benchmark, monkeypatch):
    source, candidate_id = benchmark
    destination = ProjectStore.initialize(tmp_path / "destination", title="New", lyrics=LYRICS,
        style_prompt="hip hop", target_duration_seconds=30)
    target = destination.root / "artifacts" / "benchmarks" / "collision.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing audio")
    monkeypatch.setattr(comparison, "new_id", lambda prefix: "collision")
    with pytest.raises(FileExistsError):
        comparison.import_benchmark(source, candidate_id, destination)
    assert target.read_bytes() == b"existing audio" and destination.load()["artifacts"] == []


def test_timeout_records_first_attempt_and_never_submits_next_recipe(tmp_path, benchmark):
    class TimedOut(FakeMusic3Client):
        def wait(self, *args, **kwargs):
            raise TimeoutError("remote computation may still be active")
    args = config(tmp_path, benchmark)
    report = comparison.run_comparison(args, client_factory=TimedOut,
        quality_backend=Reports(), execution_mode="test-double")
    assert report["status"] == "partial" and report["unsubmittedRecipes"] == ["concise"]
    assert FakeMusic3Client.submitted == [41]
    row = report["recipes"][0]
    assert row["status"] == "failed" and row["generationAttempts"] == 1
    assert row["actualEngineRequests"][0]["parameters"]["seed"] == 41
    assert row["submissionEvidence"][0]["remoteTaskId"]


@pytest.mark.parametrize("change,error", [("duration", "audio_duration"), ("words", "sung words")])
def test_changed_frozen_conditions_are_failed_before_export(tmp_path, benchmark, monkeypatch, change, error):
    generate = comparison.generate_candidates
    def changed(root, **kwargs):
        result = generate(root, **kwargs)
        store = ProjectStore(root)
        with store.transaction() as project:
            request = next(request for request in reversed(project["requests"]) if request["adapter"] == "minimax-music3-mlx")
            if change == "duration":
                request["parameters"]["audio_duration"] = 45
            else:
                request["parameters"]["lyrics"] = "[Verse]\nDifferent words"
        return result
    monkeypatch.setattr(comparison, "generate_candidates", changed)
    args = config(tmp_path, benchmark)
    report = run(args)
    assert report["status"] == "partial" and report["unsubmittedRecipes"] == ["concise"]
    assert error in report["recipes"][0]["error"]
    assert not report["recipes"][0].get("export") and not (args.output_dir / "native.wav").exists()


def test_interrupt_persists_terminal_report_and_stops_remaining_recipes(tmp_path, benchmark, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(comparison, "generate_candidates", interrupt)
    args = config(tmp_path, benchmark)
    report = run(args)
    assert report["status"] == "interrupted" and report["unsubmittedRecipes"] == ["concise"]
    assert report["recipes"][0]["status"] == "interrupted"
    assert json.loads((args.output_dir / "report.json").read_text()) == report
    assert FakeMusic3Client.submitted == []


@pytest.mark.parametrize("violation", ["human_review", "reuse"])
def test_generated_human_approval_or_audio_reuse_is_rejected_before_export(tmp_path, benchmark, monkeypatch, violation):
    generate = comparison.generate_candidates
    def changed(root, **kwargs):
        result = generate(root, **kwargs)
        if violation == "reuse":
            result["reusedCandidateIds"] = result["candidateIds"]
        else:
            review_candidate(root, result["candidateIds"][0], status="approved", rating=5)
        return result
    monkeypatch.setattr(comparison, "generate_candidates", changed)
    args = config(tmp_path, benchmark)
    report = run(args)
    assert report["status"] == "partial" and report["unsubmittedRecipes"] == ["concise"]
    assert not (args.output_dir / "native.wav").exists()
    assert report["musicalMinimumVerdict"] == "pending_human_listening"
    assert "reusing audio" in report["recipes"][0]["error"] if violation == "reuse" else "human listening verdict" in report["recipes"][0]["error"]
