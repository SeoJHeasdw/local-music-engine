"""Read-only views of project manifests for the CLI and the desktop app."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .jobs import ACTIVE_STATUSES, GENERATION_KINDS, frozen_batch_payloads
from .storage import PROJECT_FILENAME, ProjectStore

TOP_LEVEL_JOB_KINDS = {"candidate-batch", "repaint-candidate", "export"}


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
                "findings": [
                    findings[finding_id]
                    for finding_id in candidate["findingIds"]
                    if finding_id in findings
                ],
                "stylePrompt": parameters.get("prompt"),
                "lyrics": parameters.get("lyrics"),
                "model": parameters.get("model"),
                "bpm": parameters.get("bpm"),
                "keyScale": parameters.get("key_scale"),
                "repaintStrength": parameters.get("repaint_strength"),
                "instruction": parameters.get("instruction"),
                "feedbackId": feedback_id,
                "createdAt": candidate.get("createdAt"),
                "path": str(store.resolve_artifact(artifact)),
            }
        )
    return rows


def _job_view(project: dict[str, Any], job: dict[str, Any], generation_active: bool) -> dict[str, Any]:
    parameters = job.get("parameters", {})
    children = [record for record in project["jobs"] if record.get("parentJobId") == job["jobId"]]
    interrupted = job["kind"] in GENERATION_KINDS and job["status"] in ACTIVE_STATUSES and not generation_active
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
    }
    if job["kind"] == "candidate-batch":
        resumed_by = next((item["jobId"] for item in reversed(project["jobs"])
                           if item["parameters"].get("resumeOfJobId") == job["jobId"]), None)
        blocked = None
        try:
            frozen_batch_payloads(project, job)
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
        internal = store.resolve_artifact(artifact)
        external = artifact.get("externalPath")
        rows.append(
            {
                "artifactId": artifact["artifactId"],
                "candidateId": candidate_by_export.get(artifact["artifactId"]),
                "createdAt": artifact.get("createdAt"),
                "path": str(internal),
                "exists": internal.is_file(),
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
    return {
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
        },
        "selectedCandidateId": project.get("selectedCandidateId"),
        "canUndoSelection": _can_undo_selection(project),
        "generationActive": generation_active,
        "candidates": candidate_rows(store, project),
        "feedback": project.get("feedback", []),
        "jobs": [
            _job_view(project, job, generation_active)
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
        interrupted = [
            job for job in project["jobs"]
            if job["kind"] in GENERATION_KINDS and not job.get("parentJobId")
            and (job["status"] == "interrupted" or (job["status"] in ACTIVE_STATUSES and not generation_active))
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
            "instrumental": inputs.get("lyricsNormalized", "").strip() == "[Instrumental]",
            "versionCount": len(candidates),
            "editCount": sum(1 for item in candidates if item.get("parentCandidateId")),
            "liked": sum(1 for item in candidates if item["humanReview"]["status"] == "approved"),
            "reviewed": sum(
                1 for item in candidates if item["humanReview"]["status"] != "unreviewed"
            ),
            "selectedCandidateId": project.get("selectedCandidateId"),
            "exported": any(record.get("kind") == "export-wav" for record in project["artifacts"]),
            "runningJobs": int(generation_active),
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
