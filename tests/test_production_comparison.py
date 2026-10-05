"""Comparison contracts use test clients; these tests are not real inference."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

from local_music_engine.storage import ProjectStore
from test_auto_quality import Reports
from test_workflow import RecordingAceClient


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compare_production.py"
spec = importlib.util.spec_from_file_location("production_comparison", MODULE_PATH)
comparison = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = comparison
assert spec.loader
spec.loader.exec_module(comparison)


@pytest.fixture(autouse=True)
def reset_client():
    RecordingAceClient.payloads = []
    RecordingAceClient.submitted = []
    RecordingAceClient.fail_seeds = set()
    RecordingAceClient.loaded = ("acestep-v15-turbo", "acestep-5Hz-lm-4B")
    yield
    RecordingAceClient.fail_seeds = set()


def config(root: Path, **changes):
    return comparison.ComparisonConfig(output_dir=root, languages=("en",), seeds=(41, 42), **changes)


def run(root: Path, **changes):
    return comparison.run_comparison(config(root, **changes), client_factory=RecordingAceClient,
                                     quality_backend=Reports(["City lights are fading"]))


def test_paired_experiment_changes_only_two_rules_and_never_reuses(tmp_path: Path):
    output = tmp_path / "comparison"
    report = run(output)
    assert report["status"] == "completed"
    assert report["executionMode"] == "test-double"
    assert len(report["attempts"]) == 4
    assert RecordingAceClient.submitted == [41, 41, 42, 42]
    assert all(pair["controlsMatch"] for pair in report["pairedComparisons"])
    assert report["musicQuality"]["status"] == "unknown"
    assert report["musicQuality"]["automaticWinner"] is None
    assert report["humanListening"] == "unreviewed"
    assert json.loads((output / "report.json").read_text()) == report
    for first, second in zip(report["attempts"][::2], report["attempts"][1::2]):
        assert first["actualEngineRequests"][0]["parameters"]["prompt"] != second["actualEngineRequests"][0]["parameters"]["prompt"]
        first_rules = first["actualEngineRequests"][0]["parameters"]["productionRules"]["selection"]["ruleIds"]
        second_rules = second["actualEngineRequests"][0]["parameters"]["productionRules"]["selection"]["ruleIds"]
        assert set(second_rules) - set(first_rules) == set(comparison.EXTRA_RULES)
        assert comparison.controlled_parameters(first["actualEngineRequests"][0]["parameters"]) == comparison.controlled_parameters(second["actualEngineRequests"][0]["parameters"])
    for attempt in report["attempts"]:
        assert attempt["generationAttempts"] == 1
        assert attempt["postGenerationEngineHealthMatches"] is True
        assert attempt["postGenerationEngineHealth"]["loaded_lm_model"] == "acestep-5Hz-lm-4B"
        assert attempt["generationResult"]["reusedCandidateIds"] == []
        assert attempt["generationResult"]["newCandidateIds"]
        assert attempt["allArtifactsVerified"]
        assert attempt["selectedCandidateId"] is None
        assert attempt["submissionEvidence"][0]["remoteTaskId"]
        assert all(candidate["humanReview"]["status"] == "unreviewed" for candidate in attempt["candidates"])
        assert Path(attempt["export"]["externalPath"]).is_file()
        store = ProjectStore(attempt["projectPath"])
        project = store.load()
        assert project["inputs"]["lyricsOriginal"] == comparison.DEFAULT_LYRICS["en"]
        assert project["jobs"][0]["parameters"]["qualityPolicy"]["maxAttempts"] == 1
        assert any(artifact["kind"] == "candidate-audio" for artifact in project["artifacts"])


def test_existing_output_is_refused_before_even_reading_server(tmp_path: Path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("original")

    class NeverClient:
        def __init__(self, url):
            pytest.fail("existing outputs must be rejected before checking the server")

    with pytest.raises(FileExistsError):
        comparison.run_comparison(config(output), client_factory=NeverClient)
    assert marker.read_text() == "original"
    assert list(output.iterdir()) == [marker]


def test_failed_attempt_does_not_erase_finished_progress_or_generate_extra_seeds(tmp_path: Path, monkeypatch):
    RecordingAceClient.fail_seeds = {41}
    saved = []
    original = comparison.atomic_write_json

    def capture(path, value):
        original(path, value)
        saved.append(json.loads(path.read_text()))

    monkeypatch.setattr(comparison, "atomic_write_json", capture)
    output = tmp_path / "comparison"
    report = run(output)
    assert report["status"] == "completed_with_failures"
    assert [attempt["status"] for attempt in report["attempts"]] == ["failed", "failed", "succeeded", "succeeded"]
    assert RecordingAceClient.submitted == [41, 41, 42, 42]
    completed_counts = {sum(attempt["status"] != "running" for attempt in snapshot["attempts"]) for snapshot in saved}
    assert {0, 1, 2, 3, 4} <= completed_counts
    assert all(attempt["actualEngineRequests"] for attempt in report["attempts"])
    assert Path(report["attempts"][-1]["export"]["externalPath"]).is_file()


def test_interrupt_writes_complete_attempt_record_and_stops_future_conditions(tmp_path: Path, monkeypatch):
    def interrupt(root, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(comparison, "generate_candidates", interrupt)
    output = tmp_path / "comparison"
    with pytest.raises(KeyboardInterrupt):
        run(output)
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "interrupted"
    assert len(report["attempts"]) == 1
    assert report["attempts"][0]["status"] == "interrupted"
    assert "completedAt" in report["attempts"][0]
    assert RecordingAceClient.submitted == []


def test_controlled_duration_extension_is_reported_as_a_failed_condition(tmp_path: Path, monkeypatch):
    original = comparison.generate_candidates

    def changed(root, **kwargs):
        result = original(root, **kwargs)
        store = ProjectStore(root)
        with store.transaction() as project:
            project["requests"][0]["parameters"]["audio_duration"] = 45
        return result

    monkeypatch.setattr(comparison, "generate_candidates", changed)
    report = run(tmp_path / "comparison")
    assert report["status"] == "completed_with_failures"
    assert all("controlled input changed: audio_duration" in attempt["error"] for attempt in report["attempts"])
    assert all(attempt["artifacts"] for attempt in report["attempts"])
    assert all(not attempt.get("export") for attempt in report["attempts"])


@pytest.mark.parametrize("changes", [{"languages": ("ja",)}, {"seeds": (1, 1)}, {"seeds": (-1,)}, {"duration_seconds": float("nan")}, {"bpm": 301}])
def test_invalid_experiment_configuration_has_no_disk_or_server_side_effects(tmp_path: Path, changes):
    values = {"output_dir": tmp_path / "comparison", **changes}
    with pytest.raises(ValueError):
        comparison.run_comparison(comparison.ComparisonConfig(**values), client_factory=RecordingAceClient)
    assert not values["output_dir"].exists()
    assert not RecordingAceClient.submitted


def test_optional_no_rule_baseline_uses_same_manual_metadata():
    profiles = comparison.experiment_profiles(True)
    assert profiles[0]["id"] == "no-rules"
    assert profiles[0]["selection"] == {"version": 1, "presetId": None, "ruleIds": []}
    assert len(profiles) == 3


def test_cover_unloaded_lm_is_lazily_initialized_and_verified_after_generation(tmp_path: Path):
    class LazyLMClient(RecordingAceClient):
        initialized = False

        def health(self):
            return {**super().health(), "models_initialized": True,
                    "llm_initialized": type(self).initialized,
                    "loaded_lm_model": "acestep-5Hz-lm-4B" if type(self).initialized else None}

        def submit(self, request, source_audio=None):
            type(self).initialized = True
            return super().submit(request, source_audio)

    output = tmp_path / "comparison"
    report = comparison.run_comparison(config(output), client_factory=LazyLMClient,
                                       quality_backend=Reports(["City lights are fading"]))
    assert report["status"] == "completed"
    assert report["engine"]["llm_initialized"] is False
    assert report["engine"]["loaded_lm_model"] is None
    assert RecordingAceClient.payloads[0]["thinking"] is True
    for attempt in report["attempts"]:
        assert attempt["postGenerationEngineHealthMatches"] is True
        assert attempt["postGenerationEngineHealth"]["llm_initialized"] is True
        assert attempt["postGenerationEngineHealth"]["loaded_lm_model"] == "acestep-5Hz-lm-4B"


@pytest.mark.parametrize("health", [
    {"loaded_model": "acestep-v15-turbo", "loaded_lm_model": None},
    {"loaded_model": "acestep-v15-turbo", "loaded_lm_model": None, "llm_initialized": True},
    {"loaded_model": "acestep-v15-turbo", "loaded_lm_model": "acestep-5Hz-lm-1.7B", "llm_initialized": False},
    {"loaded_model": "another-dit", "loaded_lm_model": None, "llm_initialized": False},
])
def test_initial_lm_exception_requires_explicit_uninitialized_state_and_correct_dit(tmp_path: Path, health):
    with pytest.raises(RuntimeError):
        comparison.validate_initial_engine_health(health, config(tmp_path / "comparison"))


def test_wrong_post_generation_lm_rejects_condition_but_preserves_completed_audio(tmp_path: Path):
    class WrongPostLMClient(RecordingAceClient):
        def health(self):
            return {**super().health(), "models_initialized": True, "llm_initialized": True,
                    "loaded_lm_model": "wrong-lm" if RecordingAceClient.submitted else "acestep-5Hz-lm-4B"}

    output = tmp_path / "comparison"
    report = comparison.run_comparison(config(output), client_factory=WrongPostLMClient,
                                       quality_backend=Reports(["City lights are fading"]))
    first = report["attempts"][0]
    assert report["status"] == "completed_with_failures"
    assert first["status"] == "failed"
    assert "post-generation engine health" in first["error"]
    assert first["postGenerationEngineHealth"]["loaded_lm_model"] == "wrong-lm"
    assert first["postGenerationEngineHealthMatches"] is False
    assert first["generationResult"]["status"] == "succeeded"
    assert first["artifacts"] and first["allArtifactsVerified"]
    assert report["pairedComparisons"][0]["controlsMatch"] is None
