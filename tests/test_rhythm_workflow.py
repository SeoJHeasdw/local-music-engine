"""Generation-time structure QC uses frozen inputs and each actual artifact."""

from copy import deepcopy
import wave

import pytest

from local_music_engine import auto_quality
from local_music_engine.auto_quality import quality_policy
from local_music_engine.cli import build_parser, run
from local_music_engine.storage import ProjectStore, sha256_file
from local_music_engine.workflow import generate_candidates, resume_latest_batch, revise_inputs
from test_auto_quality import Reports
from test_workflow import FakeAceClient


class CleanEdgesClient(FakeAceClient):
    def download(self, file_path, destination):
        super().download(file_path, destination)
        with wave.open(str(destination), "rb") as audio:
            params = audio.getparams()
            frames = audio.readframes(audio.getnframes())
        with wave.open(str(destination), "wb") as audio:
            audio.setparams(params)
            audio.writeframes(b"\0\0" + frames[2:-2] + b"\0\0")


def structure_report(status="observed"):
    findings = [] if status != "needs_review" else [{
        "check": "tempo_mismatch", "severity": "warning", "message": "요청한 BPM과 추정 박자가 달라요.",
        "observed": {"estimatedBpm": 110}, "threshold": {}, "retryEligible": False,
        "confidence": 0.8, "startSeconds": 1.0, "endSeconds": 8.0,
    }]
    return {"version": auto_quality.RHYTHM_VERSION, "status": status,
            "requestedBpm": 84, "estimatedBpm": None if status == "unknown" else 110,
            "confidence": 0 if status == "unknown" else 0.8, "findings": findings}


def create_song(tmp_path):
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    return ProjectStore.initialize(tmp_path / "song", title="리듬 검사", lyrics="[Instrumental]",
        style_prompt="steady drum groove", target_duration_seconds=10, bpm=84, time_signature="6/8")


@pytest.mark.parametrize("status, expected", [("observed", "passed"), ("needs_review", "attention"), ("unknown", "unknown")])
def test_rhythm_observations_never_spend_retry_budget_or_approve_human_review(tmp_path, monkeypatch, status, expected):
    store = create_song(tmp_path)
    measured_paths = []

    def inspect(path, **kwargs):
        measured_paths.append(path)
        return structure_report(status)

    monkeypatch.setattr(auto_quality, "analyze_music_structure", inspect)
    policy = quality_policy("audio", max_attempts=2)
    policy.pop("diagnosticsVersion")  # Frozen first-stage structure policy, before attributed diagnosis.
    result = generate_candidates(store.root, seeds=[11], quality=policy, quality_backend=Reports(),
        client_factory=CleanEdgesClient, engine="ace-step")
    project = store.load()
    assert len(FakeAceClient.submitted) == 1
    raw, finished = project["candidates"]
    assert len(measured_paths) == 2 and measured_paths[0] != measured_paths[1]
    assert finished["candidateId"] == result["candidateIds"][0]
    for candidate in (raw, finished):
        report = candidate["quality"]
        artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
        assert report["status"] == expected and report["retryReasons"] == []
        assert report["rhythm"]["measuredArtifactSha256"] == artifact["sha256"]
        assert candidate["humanReview"]["status"] == "unreviewed"
        findings = [finding for finding in project["findings"] if finding["findingId"] in candidate["findingIds"]]
        assert all(finding["artifactId"] == artifact["artifactId"] for finding in findings)
        if status == "needs_review":
            assert any(finding["check"] == "tempo_mismatch" and finding["startSeconds"] == 1 for finding in findings)
    assert all(store.verify_artifact(artifact)[0] for artifact in project["artifacts"])


def test_generation_rhythm_uses_frozen_request_even_after_project_inputs_change(tmp_path, monkeypatch):
    store = create_song(tmp_path)
    calls = []

    class InputChanged(CleanEdgesClient):
        def download(self, file_path, destination):
            super().download(file_path, destination)
            revise_inputs(store.root, bpm=116, time_signature="3/4", style_prompt="rubato piano")

    def inspect(path, **kwargs):
        calls.append(kwargs)
        return structure_report()

    monkeypatch.setattr(auto_quality, "analyze_music_structure", inspect)
    generate_candidates(store.root, seeds=[12], quality=quality_policy("audio", max_attempts=1),
        quality_backend=Reports(), client_factory=InputChanged, engine="ace-step")
    assert store.load()["inputs"]["bpm"] == 116
    assert len(calls) == 2
    assert all(call == {"requested_bpm": 84, "time_signature": "6", "style_prompt": "steady drum groove"} for call in calls)


def test_failed_rhythm_inspector_keeps_audio_and_lyrics_results_without_retry(tmp_path, monkeypatch):
    store = create_song(tmp_path)

    def failed(*args, **kwargs):
        raise RuntimeError("DSP unavailable")

    monkeypatch.setattr(auto_quality, "analyze_music_structure", failed)
    result = generate_candidates(store.root, seeds=[13], quality=quality_policy("audio", max_attempts=4),
        quality_backend=Reports(), client_factory=CleanEdgesClient, engine="ace-step")
    candidate = store.find_by_id(store.load(), "candidates", "candidateId", result["candidateIds"][0])
    assert len(FakeAceClient.submitted) == 1
    assert candidate["quality"]["status"] == "unknown"
    assert candidate["quality"]["audio"]["metrics"]["frames"] > 0
    assert candidate["quality"]["lyrics"]["status"] == "not_applicable"
    assert "DSP unavailable" in candidate["quality"]["rhythm"]["error"]
    assert candidate["quality"]["retryReasons"] == []


def test_legacy_policy_does_not_add_new_measurements_or_change_saved_semantics(tmp_path, monkeypatch):
    store = create_song(tmp_path)
    policy = deepcopy(quality_policy("audio", max_attempts=1))
    policy.pop("rhythmVersion")

    def unexpected(*args, **kwargs):
        pytest.fail("Legacy generation plan cannot silently gain a new inspector")

    monkeypatch.setattr(auto_quality, "analyze_music_structure", unexpected)
    result = generate_candidates(store.root, seeds=[14], quality=policy, quality_backend=Reports(),
        client_factory=CleanEdgesClient, engine="ace-step")
    candidate = store.find_by_id(store.load(), "candidates", "candidateId", result["candidateIds"][0])
    assert "rhythm" not in candidate["quality"]
    assert candidate["quality"]["status"] == "passed"


def test_finished_rhythm_status_refers_to_its_own_measurement(tmp_path, monkeypatch):
    store = create_song(tmp_path)
    reports = iter([structure_report("unknown"), structure_report("observed")])
    monkeypatch.setattr(auto_quality, "analyze_music_structure", lambda *args, **kwargs: next(reports))
    policy = quality_policy("audio", max_attempts=1)
    policy.pop("diagnosticsVersion")
    generate_candidates(store.root, seeds=[15], quality=policy,
        quality_backend=Reports(), client_factory=CleanEdgesClient, engine="ace-step")
    raw, finished = store.load()["candidates"]
    assert raw["quality"]["status"] == "unknown"
    assert finished["quality"]["status"] == "passed"
    assert finished["quality"]["rhythm"]["status"] == "observed"


def test_explicit_resume_does_not_repeat_completed_rhythm_measurements(tmp_path, monkeypatch):
    store = create_song(tmp_path)
    monkeypatch.setattr(auto_quality, "analyze_music_structure", lambda *args, **kwargs: structure_report())
    first = generate_candidates(store.root, seeds=[16], quality=quality_policy("audio", max_attempts=1),
        quality_backend=Reports(), client_factory=CleanEdgesClient, engine="ace-step")

    def unexpected(*args, **kwargs):
        pytest.fail("Completed verified rhythm report must be reused on explicit resume")

    class Offline(CleanEdgesClient):
        health = unexpected

    monkeypatch.setattr(auto_quality, "analyze_music_structure", unexpected)
    again = resume_latest_batch(store.root, client_factory=Offline, quality_backend=Reports(), engine="ace-step")
    assert again["reusedCandidateIds"] == first["candidateIds"]
    assert len(FakeAceClient.submitted) == 1


def test_read_only_rhythm_cli_runs_real_dsp_and_reports_wav_hash(tmp_path):
    path = tmp_path / "tone.wav"
    FakeAceClient("http://127.0.0.1:18001").download("unused", path)
    before = path.read_bytes()
    report = run(build_parser().parse_args(["analyze-rhythm", str(path), "--bpm", "84", "--time-signature", "6/8"]))
    assert report["requestedBpm"] == 84
    assert report["status"] == "unknown"
    assert report["meter"]["quarterNotesPerBar"] == 3
    assert report["measuredArtifactSha256"] == sha256_file(path)
    assert report["frameSeries"]["points"]
    assert path.read_bytes() == before
