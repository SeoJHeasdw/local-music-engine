"""Recovery and batch provenance, independent of the engine and desktop app."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .storage import ProjectStore, fingerprint

ACTIVE_STATUSES = {"queued", "running", "cancelling"}
GENERATION_KINDS = {"candidate-batch", "generate-candidate", "repaint-candidate"}


def recover_jobs(store: ProjectStore, project: dict[str, Any]) -> list[str]:
    """Caller must own the generation lock and manifest transaction.

    An abandoned local process is not proof that its remote ACE task stopped. Keep
    remoteTaskId and all completed results for diagnosis; never resubmit implicitly.
    """

    recovered = []
    for job in project["jobs"]:
        if job["kind"] in GENERATION_KINDS and job["status"] in ACTIVE_STATUSES:
            store.transition_job(
                job, "interrupted", stage="interrupted",
                error="Local generation process ended before recording completion; remote task may still run",
            )
            recovered.append(job["jobId"])
    return recovered


def recover_project(project_root: str) -> dict[str, Any]:
    store = ProjectStore(project_root)
    with store.generation_lock(), store.transaction() as project:
        recovered = recover_jobs(store, project)
    return {"interruptedJobIds": recovered}


def resume_source(project: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
    if job_id is not None:
        job = ProjectStore.find_by_id(project, "jobs", "jobId", job_id)
        if job["kind"] != "candidate-batch":
            raise ValueError("only a candidate batch can be resumed")
        return job
    for job in reversed(project["jobs"]):
        if job["kind"] == "candidate-batch":
            return job
    raise ValueError("no candidate batch exists to resume")


def frozen_batch_payloads(project: dict[str, Any], batch: dict[str, Any]) -> list[dict[str, Any]]:
    """Read original parameters, including seeds not yet submitted when it crashed.

    Old manifests lack a batch snapshot. Their first saved child request is enough
    to reconstruct the other seeds. If even that is absent, refuse to guess from
    today's mutable project inputs.
    """

    parameters = batch["parameters"]
    seeds = parameters["seeds"]
    saved = parameters.get("frozenPayloads")
    if saved is not None:
        if not isinstance(saved, list) or [item.get("seed") for item in saved] != seeds:
            raise ValueError("batch frozen payloads do not match its seeds")
        return deepcopy(saved)
    requests = {item["requestId"]: item["parameters"] for item in project["requests"]}
    children = [item for item in project["jobs"] if item.get("parentJobId") == batch["jobId"]]
    by_seed = {
        item["parameters"]["seed"]: requests[item["parameters"]["requestId"]]
        for item in children if item["parameters"].get("requestId") in requests
    }
    if not by_seed:
        raise ValueError("original batch inputs are unavailable; start a new generation explicitly")
    template = next(iter(by_seed.values()))
    return [deepcopy(by_seed.get(seed, {**template, "seed": seed})) for seed in seeds]


def batch_lineage(project: dict[str, Any], batch: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    current = batch
    while True:
        if current["jobId"] in ids:
            raise ValueError("cyclic batch resume history")
        ids.add(current["jobId"])
        previous = current["parameters"].get("resumeOfJobId")
        if not previous:
            return ids
        current = resume_source(project, previous)


def reusable_candidates(
    store: ProjectStore, project: dict[str, Any], batch: dict[str, Any],
    payloads: list[dict[str, Any]],
) -> dict[int, str]:
    """Reuse only verified outputs of this batch's resume chain, never other runs."""

    lineage = batch_lineage(project, batch)
    jobs = {item["jobId"]: item for item in project["jobs"]}
    artifacts = {item["artifactId"]: item for item in project["artifacts"]}
    requests = {item["requestId"]: item for item in project["requests"]}
    expected = {item["seed"]: fingerprint(item) for item in payloads}
    reusable = {}
    for candidate in project["candidates"]:
        if candidate["status"] != "ready":
            continue
        artifact = artifacts[candidate["artifactId"]]
        job = jobs.get(artifact.get("createdByJobId"))
        if job is None or job.get("parentJobId") not in lineage or job["status"] != "succeeded":
            continue
        payload = requests[candidate["requestId"]]["parameters"]
        seed = payload.get("seed")
        if expected.get(seed) != fingerprint(payload):
            continue
        valid, _ = store.verify_artifact(artifact)
        if valid:
            reusable[seed] = candidate["candidateId"]
    return reusable
