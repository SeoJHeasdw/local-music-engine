"""Listening copies: the mastered file of a version that the app plays and export writes.

Each copy is its own artifact (``listening-audio``) and job (``listening-copy``) that
points at the version's audio; the version itself is never changed. Why and how the
copy is mastered is in :mod:`.mastering`. A copy is a render that can be made again from
the version, so when mastering changes, the new copy retires the old one: its file is
removed and its record says what replaced it. A version keeps one copy on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .jobs import ACTIVE_STATUSES, cleanup_export_outputs, recover_jobs
from .mastering import MASTERING_VERSION, master_audio
from .qc import artifact_and_findings
from .storage import ProjectStore, new_id, sha256_file, utc_now

LISTENING_KIND = "listening-audio"


def listening_copies(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The newest listening copy of each source artifact, by source artifact id."""

    return {artifact["sourceArtifactId"]: artifact for artifact in project["artifacts"]
            if artifact.get("kind") == LISTENING_KIND and artifact.get("sourceArtifactId") and not artifact.get("retired")}


def _current(copy: dict[str, Any]) -> bool:
    return copy["mastering"].get("version") == MASTERING_VERSION


def _inherited_drive(project: dict[str, Any], candidate: dict[str, Any], copies: dict[str, dict[str, Any]]) -> float | None:
    """An edit keeps the level of the mastered version it edits."""

    if candidate.get("editRange") is None:
        return None
    candidates = {item["candidateId"]: item for item in project["candidates"]}
    seen = {candidate["candidateId"]}
    parent = candidates.get(candidate.get("parentCandidateId") or "")
    while parent is not None and parent["candidateId"] not in seen:
        copy = copies.get(parent["artifactId"])
        if copy is not None and _current(copy):
            return copy["mastering"]["driveDb"]
        seen.add(parent["candidateId"])
        parent = candidates.get(parent.get("parentCandidateId") or "")
    return None


def ensure_listening_copy(store: ProjectStore, candidate_id: str, *, parent_job_id: str | None = None) -> str:
    """Return the version's listening copy, mastering one if it has none that verifies."""

    project = store.load()
    candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    source_artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
    copies = listening_copies(project)
    existing = copies.get(source_artifact["artifactId"])
    if existing is not None and _current(existing) and store.verify_artifact(existing)[0]:
        return existing["artifactId"]
    valid, reason = store.verify_artifact(source_artifact)
    if not valid:
        raise ValueError(reason)
    source = store.resolve_artifact(source_artifact)
    drive = _inherited_drive(project, candidate, copies)
    with store.transaction() as project:
        job = store.append_job(project, kind="listening-copy", parent_job_id=parent_job_id, parameters={
            "candidateId": candidate_id, "sourceArtifactId": source_artifact["artifactId"], "inheritedDriveDb": drive})
        job["ownedOutputs"] = []
        store.transition_job(job, "running", stage="finalizing: mastering")
    job_id = job["jobId"]
    destination = store.root / "artifacts" / "listening" / f"{job_id}.wav"
    try:
        def staged(record: dict[str, Any]) -> None:
            with store.transaction() as project:
                store.find_by_id(project, "jobs", "jobId", job_id)["ownedOutputs"] = [record]

        audio = source_artifact["audio"]
        mastering = master_audio(source, destination, channels=audio["channels"], sample_rate=audio["sampleRate"],
                                 duration_seconds=audio["durationSeconds"], drive_db=drive, on_staged=staged)
        # The version carries the checks; the copy records only what mastering did and measured.
        artifact, _ = artifact_and_findings(path=destination, project_relative_path=store.relative_path(destination),
                                            artifact_kind=LISTENING_KIND, created_by_job_id=job_id,
                                            requested_duration_seconds=audio["durationSeconds"])
        artifact.update(sourceArtifactId=source_artifact["artifactId"], sourceArtifactSha256=source_artifact["sha256"],
                        mastering=mastering)
        with store.transaction() as project:
            retired = [old for old in project["artifacts"] if old.get("kind") == LISTENING_KIND and not old.get("retired")
                       and old.get("sourceArtifactId") == source_artifact["artifactId"]]
            for old in retired:
                old["retired"] = {"supersededBy": artifact["artifactId"], "at": utc_now()}
            project["artifacts"].append(artifact)
            project["revisions"].append({"revisionId": new_id("revision"), "kind": "listening-copy",
                "previousRevisionId": project["revisions"][-1]["revisionId"] if project["revisions"] else None,
                "before": {"candidateId": candidate_id, "artifactId": source_artifact["artifactId"]},
                "after": {"candidateId": candidate_id, "listeningArtifactId": artifact["artifactId"]},
                "createdAt": utc_now()})
            current = store.find_by_id(project, "jobs", "jobId", job_id)
            current["resultRefs"] = [artifact["artifactId"]]
            store.transition_job(current, "succeeded", stage="mastered", progress=1)
        cleanup_export_outputs(current, include_published=False)
        for old in retired:
            remove_retired(store, old)
        return artifact["artifactId"]
    except (Exception, KeyboardInterrupt) as error:
        try:
            with store.transaction() as project:
                current = store.find_by_id(project, "jobs", "jobId", job_id)
                if current["status"] in ACTIVE_STATUSES:
                    current["outputCleanupErrors"] = cleanup_export_outputs(current)
                    state = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
                    store.transition_job(current, state, stage=state, error=f"{type(error).__name__}: {error}")
        except (Exception, KeyboardInterrupt) as recording_error:
            error.add_note(f"Could not record listening copy failure: {recording_error}")
        raise


def remove_retired(store: ProjectStore, artifact: dict[str, Any]) -> None:
    """Delete a retired copy's file only while it is still exactly the file that was recorded."""

    try:
        path = store.resolve_artifact(artifact)
        if path.is_file() and not path.is_symlink() and sha256_file(path) == artifact["sha256"]:
            path.unlink()
    except (OSError, ValueError):
        pass  # A file that was moved or changed is left alone.


def played_versions(project: dict[str, Any]) -> list[str]:
    """Every version the app can play, except a take that a finished copy replaced."""

    requests = {request["requestId"]: request for request in project["requests"]}
    replaced = {candidate.get("parentCandidateId") for candidate in project["candidates"]
                if requests[candidate["requestId"]]["parameters"].get("task_type") == "postprocess"}
    return [candidate["candidateId"] for candidate in project["candidates"] if candidate["candidateId"] not in replaced]


def master_versions(project_root: Path | str, *, candidate_id: str | None = None) -> dict[str, Any]:
    """Make the missing listening copies of a song's versions, oldest first."""

    store = ProjectStore(project_root)
    with store.generation_lock(lane=False):
        with store.transaction() as project:
            recover_jobs(store, project)
        targets = [candidate_id] if candidate_id else played_versions(project)
        made, failed = {}, {}
        for target in targets:
            try:
                made[target] = ensure_listening_copy(store, target)
            except Exception as error:
                failed[target] = f"{type(error).__name__}: {error}"
    return {"listeningArtifactIds": made, "failures": failed}
