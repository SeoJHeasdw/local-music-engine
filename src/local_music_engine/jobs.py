"""Recovery and batch provenance, independent of the engine and desktop app."""

from __future__ import annotations

import stat
from copy import deepcopy
from pathlib import Path
from typing import Any

from .storage import ProjectStore, fingerprint, sha256_file

ACTIVE_STATUSES = {"queued", "running", "cancelling"}
GENERATION_KINDS = {"candidate-batch", "generate-candidate", "repaint-candidate", "quality-check", "audio-finish"}
RECOVERABLE_KINDS = GENERATION_KINDS | {"export"}


def validate_seed(seed: Any) -> int:
    """ACE replaces negative seeds with randomness; accept only its fixed seed range."""

    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer between 0 and 4294967295")
    return seed


def cleanup_export_outputs(job: dict[str, Any], *, include_published: bool = True) -> list[str]:
    """Roll back only files whose identity was saved before this job published them.

    The temporary hard link remains until the success manifest is durable. Never
    delete a symlink or a file replaced/changed by the user at the same output path.
    """

    errors = []
    for output in job.get("ownedOutputs", []):
        expected = (output["device"], output["inode"])
        for key in (("path", "temporaryPath") if include_published else ("temporaryPath",)):
            path = Path(output[key])
            try:
                current = path.lstat()
                matches_identity = stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == expected
                if key == "path":
                    try:
                        anchor = Path(output["temporaryPath"]).lstat()
                        matches_anchor = stat.S_ISREG(anchor.st_mode) and (anchor.st_dev, anchor.st_ino) == expected
                    except FileNotFoundError:
                        matches_anchor = False
                    if (not matches_identity or not matches_anchor
                            or current.st_size != output.get("expectedBytes")
                            or sha256_file(path) != output.get("expectedSha256")):
                        errors.append(f"Preserved changed or unowned export output: {path}")
                        continue
                if matches_identity:
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(f"{path}: {error}")
    return errors


def recover_export_jobs(store: ProjectStore, project: dict[str, Any]) -> list[str]:
    """Caller owns the export lease, or has established that no exporter owns it."""

    recovered = []
    for job in project["jobs"]:
        if job["kind"] != "export" or job["status"] not in ACTIVE_STATUSES:
            continue
        job["outputCleanupErrors"] = cleanup_export_outputs(job)
        error = "Local export process ended before recording completion"
        if not job.get("ownedOutputs"):
            error += "; no output ownership was recorded, so existing files were preserved"
        elif job["outputCleanupErrors"]:
            error += "; some files were preserved or could not be removed; see output cleanup warnings"
        else:
            error += "; incomplete owned outputs were cleaned up"
        store.transition_job(
            job, "interrupted", stage="interrupted",
            error=error,
        )
        recovered.append(job["jobId"])
    return recovered


def recover_jobs(store: ProjectStore, project: dict[str, Any]) -> list[str]:
    """Caller must own the generation lock and manifest transaction.

    An abandoned local process is not proof that its remote ACE task stopped. Keep
    remoteTaskId and all completed results for diagnosis; never resubmit implicitly.
    """

    recovered = [] if store.export_active() else recover_export_jobs(store, project)
    for job in project["jobs"]:
        if job["status"] == "succeeded" and (job["kind"] == "audio-finish"
                or (job["kind"] == "export" and not store.export_active())):
            # A crash can occur after success is durable but before staging links
            # are released. Historical published artifacts are never removed.
            cleanup_export_outputs(job, include_published=False)
        if job["kind"] in GENERATION_KINDS and job["status"] in ACTIVE_STATUSES:
            if job["kind"] == "audio-finish":
                job["outputCleanupErrors"] = cleanup_export_outputs(job)
            store.transition_job(
                job, "interrupted", stage="interrupted",
                error=("Local quality inspection or finishing ended before recording completion"
                       if job["kind"] in {"quality-check", "audio-finish"}
                       else "Local generation process ended before recording completion; remote task may still run"),
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
    for seed in seeds:
        validate_seed(seed)
    saved = parameters.get("frozenPayloads")
    if saved is not None:
        if not isinstance(saved, list) or [item.get("seed") for item in saved] != seeds:
            raise ValueError("batch frozen payloads do not match its seeds")
        for payload in saved:
            validate_seed(payload["seed"])
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
    payloads = [deepcopy(by_seed.get(seed, {**template, "seed": seed})) for seed in seeds]
    for seed, payload in zip(seeds, payloads, strict=True):
        validate_seed(payload.get("seed"))
        if payload["seed"] != seed:
            raise ValueError("batch original payloads do not match its seeds")
    return payloads


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
