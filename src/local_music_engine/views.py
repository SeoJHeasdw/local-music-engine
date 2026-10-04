"""Read-only views of project manifests for the CLI and the desktop app."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .jobs import ACTIVE_STATUSES, RECOVERABLE_KINDS, frozen_batch_payloads
from .music3 import CAPABILITIES, ENGINE, MAX_DURATION_SECONDS
from .lyrics import is_instrumental_lyrics
from .storage import PROJECT_FILENAME, ProjectStore

TOP_LEVEL_JOB_KINDS = {"candidate-batch", "cover-batch", "repaint-candidate", "export"}


def _artifact_path(store: ProjectStore, artifact: dict[str, Any]) -> Path | None:
    try:
        return store.resolve_artifact(artifact)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return None


def candidate_rows(store: ProjectStore, project: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per candidate, with the exact inputs its request froze.

    Every artifact is re-hashed here; callers that poll should read the manifest instead.
    """

    artifacts = {record["artifactId"]: record for record in project["artifacts"]}
    requests = {record["requestId"]: record for record in project["requests"]}
    findings = {record["findingId"]: record for record in project["findings"]}
    jobs = {record["jobId"]: record for record in project["jobs"]}
    feedback_by_job = {
        record["jobId"]: record["feedbackId"] for record in project.get("feedback", [])
    }
    rows = []
    for candidate in project["candidates"]:
        artifact = artifacts[candidate["artifactId"]]
        valid, reason = store.verify_artifact(artifact)
        path = _artifact_path(store, artifact)
        request = requests[candidate["requestId"]]
        parameters = request["parameters"]
        job = jobs.get(artifact.get("createdByJobId") or "")
        feedback_id = None
        if job is not None:
            batch = jobs.get(job.get("parentJobId") or "", {})
            feedback_id = (feedback_by_job.get(job["jobId"])
                           or job["parameters"].get("feedbackId")
                           or feedback_by_job.get(job.get("parentJobId") or "")
                           or batch.get("parameters", {}).get("feedbackId"))
        rows.append(
            {
                "candidateId": candidate["candidateId"],
                "selected": candidate["candidateId"] == project.get("selectedCandidateId"),
                "seed": parameters.get("seed"),
                "taskType": parameters.get("task_type"),
                "parentCandidateId": candidate.get("parentCandidateId"),
                "editRange": candidate.get("editRange"),
                "durationSeconds": artifact.get("audio", {}).get("durationSeconds"),
                "artifactValid": valid,
                "artifactValidation": reason,
                "humanReview": candidate["humanReview"],
                "quality": candidate.get("quality"),
                "recommended": candidate["candidateId"] == project.get("recommendedCandidateId"),
                "findings": [
                    findings[finding_id]
                    for finding_id in candidate["findingIds"]
                    if finding_id in findings
                ],
                "stylePrompt": parameters.get("prompt"),
                "baseStylePrompt": parameters.get("productionRules", {}).get("baseStylePrompt", parameters.get("sourceStylePrompt", parameters.get("prompt"))),
                "productionRules": parameters.get("productionRules"),
                "songPlan": parameters.get("songPlan"),
                "lyricsOriginal": parameters.get("songPlan", {}).get("lyricsOriginal", parameters.get("sourceLyricsOriginal", parameters.get("lyrics"))),
                "coverStrength": parameters.get("audio_cover_strength") if parameters.get("coverSource") else None,
                "coverSource": parameters.get("coverSource"),
                "lyrics": parameters.get("lyrics"),
                "model": parameters.get("model"),
                "engine": parameters.get("engine", "ace-step"),
                "requestedDurationSeconds": parameters.get("audio_duration"),
                "actualDurationSeconds": artifact.get("audio", {}).get("durationSeconds"),
                "bpm": parameters.get("bpm"),
                "keyScale": parameters.get("key_scale"),
                "timeSignature": parameters.get("time_signature"),
                "repaintStrength": parameters.get("repaint_strength"),
                "instruction": parameters.get("instruction"),
                "feedbackId": feedback_id,
                "createdAt": candidate.get("createdAt"),
                "path": str(path) if path is not None else "",
            }
        )
    return rows


def _job_view(project: dict[str, Any], job: dict[str, Any], generation_active: bool, export_active: bool) -> dict[str, Any]:
    parameters = job.get("parameters", {})
    children = [record for record in project["jobs"] if record.get("parentJobId") == job["jobId"]]
    owner_active = export_active if job["kind"] == "export" else generation_active
    interrupted = job["kind"] in RECOVERABLE_KINDS and job["status"] in ACTIVE_STATUSES and not owner_active
    view = {
        "jobId": job["jobId"],
        "kind": job["kind"],
        "status": "interrupted" if interrupted else job["status"],
        "storedStatus": job["status"],
        "stage": job.get("stage"),
        "progress": job.get("progress", 0.0),
        "error": job.get("error"),
        "createdAt": job.get("createdAt"),
        "startedAt": job.get("startedAt"),
        "finishedAt": job.get("finishedAt"),
        "feedbackId": parameters.get("feedbackId"),
        "resultRefs": job.get("resultRefs", []),
        "unsubmittedSeeds": job.get("unsubmittedSeeds", []),
        "stopReason": job.get("stopReason"),
    }
    if job["kind"] in {"candidate-batch", "cover-batch"}:
        resumed_by = next((item["jobId"] for item in reversed(project["jobs"])
                           if item["parameters"].get("resumeOfJobId") == job["jobId"]), None)
        blocked = None
        try:
            frozen = frozen_batch_payloads(project, job)
            if any(item.get("engine") != ENGINE for item in frozen):
                blocked = "과거 ACE 작업은 Music 3에서 이어 만들 수 없어요. 가사와 스타일로 새 곡을 만들어 주세요."
        except ValueError as error:
            blocked = str(error)
        view.update(
            {
                "seeds": parameters.get("seeds", []),
                "succeeded": sum(1 for child in children if child["status"] == "succeeded"),
                "failed": sum(1 for child in children if child["status"] == "failed"),
                "reused": len(job.get("reusedCandidateIds", [])),
                "failures": job.get("failures", []),
                "resumeOfJobId": parameters.get("resumeOfJobId"),
                "resumedByJobId": resumed_by,
                "canResume": not generation_active and not resumed_by and not blocked
                             and view["status"] in {"interrupted", "failed", "partial", "cancelled"},
                "resumeBlockedReason": blocked,
            }
        )
    elif job["kind"] == "repaint-candidate":
        view["parentCandidateId"] = parameters.get("parentCandidateId")
    elif job["kind"] == "export":
        view["candidateId"] = parameters.get("candidateId")
        view["outputCleanupErrors"] = job.get("outputCleanupErrors", [])
    return view


def _exports(store: ProjectStore, project: dict[str, Any]) -> list[dict[str, Any]]:
    candidate_by_export = {
        revision["after"].get("exportArtifactId"): revision["after"].get("candidateId")
        for revision in project["revisions"]
        if revision["kind"] == "export"
    }
    rows = []
    for artifact in project["artifacts"]:
        if artifact.get("kind") != "export-wav":
            continue
        internal = _artifact_path(store, artifact)
        external = artifact.get("externalPath")
        rows.append(
            {
                "artifactId": artifact["artifactId"],
                "candidateId": candidate_by_export.get(artifact["artifactId"]),
                "createdAt": artifact.get("createdAt"),
                "path": str(internal) if internal is not None else "",
                "exists": internal is not None and internal.is_file(),
                "externalPath": external,
                "externalExists": bool(external) and Path(external).is_file(),
            }
        )
    return rows


def _can_undo_selection(project: dict[str, Any]) -> bool:
    return any(
        revision["kind"] == "candidate-selection" and not revision.get("undoneByRevisionId")
        for revision in project["revisions"]
    )


def project_status(store: ProjectStore, project: dict[str, Any]) -> dict[str, Any]:
    inputs = project["inputs"]
    generation_active = store.generation_active()
    export_active = store.export_active()
    return {
        "engine": ENGINE,
        "capabilities": dict(CAPABILITIES),
        "maxDurationSeconds": MAX_DURATION_SECONDS,
        "projectId": project["projectId"],
        "title": project["title"],
        "path": str(store.root),
        "createdAt": project.get("createdAt"),
        "updatedAt": project.get("updatedAt"),
        "inputs": {
            "lyrics": inputs.get("lyricsOriginal", ""),
            "stylePrompt": inputs.get("stylePrompt", ""),
            "durationSeconds": inputs.get("targetDurationSeconds"),
            "structure": inputs.get("structure"),
            "bpm": inputs.get("bpm"),
            "keyScale": inputs.get("keyScale"),
            "timeSignature": inputs.get("timeSignature"),
            "productionRules": inputs.get("productionRules"),
        },
        "selectedCandidateId": project.get("selectedCandidateId"),
        "recommendedCandidateId": project.get("recommendedCandidateId"),
        "canUndoSelection": _can_undo_selection(project),
        "generationActive": generation_active,
        "candidates": candidate_rows(store, project),
        "feedback": project.get("feedback", []),
        "jobs": [
            _job_view(project, job, generation_active, export_active)
            for job in project["jobs"]
            if job["kind"] in TOP_LEVEL_JOB_KINDS and not job.get("parentJobId")
        ],
        "exports": _exports(store, project),
        "revisions": [
            {key: revision.get(key) for key in ("revisionId", "kind", "before", "after", "reason", "createdAt")}
            for revision in project["revisions"]
        ],
    }


def _summary(path: Path) -> dict[str, Any]:
    manifest = path / PROJECT_FILENAME
    try:
        project = json.loads(manifest.read_text(encoding="utf-8"))
        inputs = project["inputs"]
        candidates = project["candidates"]
        generation_active = ProjectStore(path).generation_active()
        export_active = ProjectStore(path).export_active()
        interrupted = [
            job for job in project["jobs"]
            if job["kind"] in RECOVERABLE_KINDS and not job.get("parentJobId")
            and (job["status"] == "interrupted" or (job["status"] in ACTIVE_STATUSES
                 and not (export_active if job["kind"] == "export" else generation_active)))
        ]
        return {
            "path": str(path),
            "folderName": path.name,
            "projectId": project["projectId"],
            "title": project.get("title") or path.name,
            "createdAt": project.get("createdAt"),
            "updatedAt": project.get("updatedAt"),
            "stylePrompt": inputs.get("stylePrompt", ""),
            "durationSeconds": inputs.get("targetDurationSeconds"),
            "instrumental": is_instrumental_lyrics(inputs.get("lyricsNormalized", "")),
            "versionCount": sum(1 for item in candidates if not item.get("quality") or item["quality"].get("preferred")),
            "automaticAttemptCount": sum(1 for item in candidates if item.get("quality") and not item["quality"].get("preferred")),
            "editCount": sum(1 for item in candidates if item.get("parentCandidateId") and (not item.get("quality") or item["quality"].get("preferred"))
                             and (item.get("editRange") or not item.get("quality", {}).get("processing"))),
            "liked": sum(1 for item in candidates if item["humanReview"]["status"] == "approved"),
            "reviewed": sum(
                1 for item in candidates if item["humanReview"]["status"] != "unreviewed"
            ),
            "selectedCandidateId": project.get("selectedCandidateId"),
            "exported": any(record.get("kind") == "export-wav" for record in project["artifacts"]),
            "runningJobs": int(generation_active) + int(export_active),
            "interruptedJobs": len(interrupted),
            "error": None,
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {
            "path": str(path),
            "folderName": path.name,
            "projectId": None,
            "title": path.name,
            "error": f"{type(error).__name__}: {error}",
        }


def library(directory: Path | None, extra_projects: Iterable[Path] = ()) -> list[dict[str, Any]]:
    """Summaries of every project directly under ``directory`` plus explicit extras.

    No artifact is hashed; opening a song does the full verification.
    """

    seen: set[Path] = set()
    rows = []
    candidates: list[Path] = []
    if directory is not None and directory.is_dir():
        candidates.extend(sorted(child for child in directory.iterdir() if child.is_dir()))
    candidates.extend(extra_projects)
    for path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen or not (resolved / PROJECT_FILENAME).is_file():
            continue
        seen.add(resolved)
        rows.append(_summary(resolved))
    rows.sort(key=lambda row: row.get("updatedAt") or "", reverse=True)
    return rows
