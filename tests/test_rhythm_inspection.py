"""Append-only candidate inspections preserve original QC, music and human review."""

from copy import deepcopy

import pytest

from local_music_engine.auto_quality import quality_policy
from local_music_engine.rhythm_inspection import inspect_saved_candidate
from local_music_engine.storage import ProjectStore
from local_music_engine.views import candidate_rows
from local_music_engine.workflow import generate_candidates
from test_rhythm_workflow import CleanEdgesClient
from test_auto_quality import Reports
from test_workflow import FakeAceClient


def saved_song(tmp_path):
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    store = ProjectStore.initialize(tmp_path / "song", title="구간 진단", lyrics="[Instrumental]",
        style_prompt="steady drums", target_duration_seconds=10, bpm=120, time_signature="4/4")
    batch = generate_candidates(store.root, seeds=[1], client_factory=CleanEdgesClient, engine="ace-step")
    return store, batch["candidateIds"][0]


def test_inspections_append_verified_reports_without_overwriting_qc_audio_or_review(tmp_path):
    store, candidate_id = saved_song(tmp_path)
    original = deepcopy(store.load())
    candidate = store.find_by_id(original, "candidates", "candidateId", candidate_id)
    first = inspect_saved_candidate(store.root, candidate_id=candidate_id)
    second = inspect_saved_candidate(store.root, candidate_id=candidate_id)
    project = store.load()
    after = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    assert after.get("quality") == candidate.get("quality")
    assert after["humanReview"] == candidate["humanReview"]
    assert len(project["candidates"]) == len(original["candidates"])
    assert project["selectedCandidateId"] == original["selectedCandidateId"]
    assert len(project["rhythmInspections"]) == 2
    assert first["reportArtifactId"] != second["reportArtifactId"]
    assert project["revisions"][-1]["kind"] == "rhythm-inspection"
    assert all(store.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    view = next(row for row in candidate_rows(store, project) if row["candidateId"] == candidate_id)
    diagnosis = view["quality"]["rhythm"]["diagnostics"]
    assert diagnosis["checks"]["beatTiming"]["status"] == "unknown"
    assert diagnosis["checks"]["backingContinuity"]["status"] == "unknown"
    assert view["quality"]["inspectionId"] == second["inspectionId"]


def test_corrupt_inspection_report_is_not_displayed_as_verified(tmp_path):
    store, candidate_id = saved_song(tmp_path)
    report = inspect_saved_candidate(store.root, candidate_id=candidate_id)
    artifact = store.find_by_id(store.load(), "artifacts", "artifactId", report["reportArtifactId"])
    store.resolve_artifact(artifact).write_text("{}")
    view = next(row for row in candidate_rows(store, store.load()) if row["candidateId"] == candidate_id)
    assert view["quality"]["status"] == "unknown"
    assert "검증" in view["quality"]["summary"]


def test_modified_source_audio_is_rejected_before_creating_an_inspection(tmp_path):
    store, candidate_id = saved_song(tmp_path)
    project = store.load()
    candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
    with store.resolve_artifact(artifact).open("ab") as stream:
        stream.write(b"changed")
    jobs = len(project["jobs"])
    with pytest.raises(ValueError, match="size|hash"):
        inspect_saved_candidate(store.root, candidate_id=candidate_id)
    assert len(store.load()["jobs"]) == jobs


def test_existing_inspection_becomes_unknown_after_source_audio_changes(tmp_path):
    store, candidate_id = saved_song(tmp_path)
    inspect_saved_candidate(store.root, candidate_id=candidate_id)
    project = store.load()
    candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
    with store.resolve_artifact(artifact).open("ab") as stream:
        stream.write(b"changed")
    row = next(row for row in candidate_rows(store, store.load()) if row["candidateId"] == candidate_id)
    assert row["artifactValid"] is False
    assert row["quality"]["status"] == "unknown"
    assert row["quality"]["rhythm"]["status"] == "unknown"


def test_new_quality_policy_runs_attributed_diagnostics_without_inventing_missing_stems(tmp_path):
    store = ProjectStore.initialize(tmp_path / "song", title="자동 구간 검사", lyrics="[Instrumental]",
        style_prompt="piano", target_duration_seconds=10)
    batch = generate_candidates(store.root, seeds=[2], client_factory=CleanEdgesClient, engine="ace-step",
        quality=quality_policy("audio", max_attempts=1), quality_backend=Reports())
    project = store.load()
    winner = store.find_by_id(project, "candidates", "candidateId", batch["candidateIds"][0])
    diagnosis = winner["quality"]["rhythm"]["diagnostics"]
    assert diagnosis["checks"]["backingContinuity"]["status"] == "unknown"
    assert diagnosis["backingEvidenceArtifactSha256"] is None
    assert winner["quality"]["retryReasons"] == []
    assert winner["humanReview"]["status"] == "unreviewed"
