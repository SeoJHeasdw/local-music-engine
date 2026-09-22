"""Run a frozen request without holding the manifest lock during inference or QC."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .ace_adapter import AceStepClient
from .jobs import ACTIVE_STATUSES
from .qc import artifact_and_findings
from .storage import ProjectStore, new_id, utc_now


def execute_candidate(
    store: ProjectStore,
    client: AceStepClient,
    *,
    request: dict[str, Any],
    job_id: str,
    api_payload: dict[str, Any],
    poll_seconds: float,
    timeout_seconds: float,
    batch_id: str | None = None,
    index: int = 0,
    total: int = 1,
    source: Path | None = None,
    parent_candidate_id: str | None = None,
    edit_range: dict[str, float] | None = None,
    context_range: dict[str, float] | None = None,
) -> str:
    seed = request["parameters"]["seed"]

    def progress(state: dict[str, Any]) -> None:
        with store.transaction() as project:
            job = store.find_by_id(project, "jobs", "jobId", job_id)
            # ACE occasionally reports stages without progress; never move backwards.
            value = max(job["progress"], min(0.99, max(0.0, float(state["progress"]))))
            job.update(stage=str(state["stage"]), progress=value)
            if batch_id:
                batch = store.find_by_id(project, "jobs", "jobId", batch_id)
                batch.update(stage=f"seed {seed}: {job['stage']}", progress=(index + value) / total)

    try:
        if source is None:
            task_id = client.submit(api_payload)
        else:
            task_id = client.submit(api_payload, source_audio=source)
        # Persist before the first poll, including when wait() fails immediately.
        with store.transaction() as project:
            job = store.find_by_id(project, "jobs", "jobId", job_id)
            job["remoteTaskId"] = task_id
        result = client.wait(
            task_id, poll_seconds=poll_seconds, timeout_seconds=timeout_seconds,
            on_progress=progress,
        )
        file_path = str(result.get("file") or "")
        if not file_path:
            raise RuntimeError("ACE task succeeded without an audio file")
        progress({"stage": "downloading", "progress": 0.97})
        directory = "repaints" if parent_candidate_id else "candidates"
        destination = store.root / "artifacts" / directory / f"{job_id}-seed-{seed}.wav"
        # Resolve before writing as well as after: a symlinked artifact directory must
        # not turn the engine into a writer outside the project.
        relative = store.relative_path(destination)
        client.download(file_path, destination)
        progress({"stage": "checking audio", "progress": 0.99})
        artifact, findings = artifact_and_findings(
            path=destination,
            project_relative_path=relative,
            artifact_kind="candidate-audio",
            created_by_job_id=job_id,
            requested_duration_seconds=float(request["parameters"]["audio_duration"]),
        )
        candidate = {
            "candidateId": new_id("candidate"),
            "requestId": request["requestId"],
            "artifactId": artifact["artifactId"],
            "findingIds": [finding["findingId"] for finding in findings],
            "status": "ready",
            "parentCandidateId": parent_candidate_id,
            "editRange": deepcopy(edit_range),
            "contextRange": deepcopy(context_range),
            "humanReview": {"status": "unreviewed", "rating": None, "notes": [], "updatedAt": None},
            "createdAt": utc_now(),
        }
        with store.transaction() as project:
            job = store.find_by_id(project, "jobs", "jobId", job_id)
            project["artifacts"].append(artifact)
            project["findings"].extend(findings)
            project["candidates"].append(candidate)
            job["resultRefs"] = [candidate["candidateId"], artifact["artifactId"]]
            job["remoteResult"] = {
                key: result.get(key)
                for key in ("generation_info", "seed_value", "lm_model", "dit_model", "metas")
            }
            store.transition_job(job, "succeeded", stage="verified", progress=1.0)
            if batch_id:
                batch = store.find_by_id(project, "jobs", "jobId", batch_id)
                batch["resultRefs"].append(candidate["candidateId"])
                batch["progress"] = (index + 1) / total
        return candidate["candidateId"]
    except (Exception, KeyboardInterrupt) as error:
        with store.transaction() as project:
            job = store.find_by_id(project, "jobs", "jobId", job_id)
            if job["status"] in ACTIVE_STATUSES:
                cancelled = isinstance(error, KeyboardInterrupt)
                job["cancelRequested"] = cancelled
                state = "cancelled" if cancelled else "failed"
                store.transition_job(job, state, stage=state, error=f"{type(error).__name__}: {error}")
        raise
