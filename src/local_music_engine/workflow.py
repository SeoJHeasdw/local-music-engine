"""Project workflows shared by the CLI and the future app server."""

from __future__ import annotations

import os
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterable

from .ace_adapter import AceStepClient
from .qc import artifact_and_findings, inspect_wav
from .storage import ProjectStore, fingerprint, new_id, sha256_file, utc_now

DEFAULT_BASE_URL = "http://127.0.0.1:18001"
DEFAULT_DIT_MODEL = "acestep-v15-turbo"
DEFAULT_LM_MODEL = "acestep-5Hz-lm-0.6B"


def _frozen_generation_payload(
    project: dict[str, Any],
    *,
    seed: int,
    model: str,
    lm_model: str,
    task_type: str = "text2music",
    parent_artifact_sha256: str | None = None,
    edit_range: dict[str, float] | None = None,
    instruction: str | None = None,
) -> dict[str, Any]:
    inputs = project["inputs"]
    payload: dict[str, Any] = {
        "prompt": inputs["stylePrompt"],
        "lyrics": inputs["lyricsNormalized"],
        "thinking": task_type == "text2music",
        "vocal_language": "ko",
        "audio_format": "wav",
        "audio_duration": float(inputs["targetDurationSeconds"]),
        "inference_steps": 8,
        "use_random_seed": False,
        "seed": int(seed),
        "batch_size": 1,
        "model": model,
        "lm_model_path": lm_model,
        "lm_backend": "mlx",
        "task_type": task_type,
        "project_structure": inputs.get("structure"),
    }
    if task_type == "text2music":
        payload.update(
            {
                "use_cot_caption": False,
                "use_cot_language": False,
                "constrained_decoding": True,
            }
        )
    if edit_range is not None:
        payload["repainting_start"] = edit_range["startSeconds"]
        payload["repainting_end"] = edit_range["endSeconds"]
    if instruction:
        payload["instruction"] = instruction
    if parent_artifact_sha256:
        # Provenance only; the adapter removes it before calling the ACE API.
        payload["source_artifact_sha256"] = parent_artifact_sha256
    return payload


def _api_payload(frozen: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "prompt",
        "lyrics",
        "thinking",
        "vocal_language",
        "audio_format",
        "audio_duration",
        "inference_steps",
        "use_random_seed",
        "seed",
        "batch_size",
        "model",
        "lm_model_path",
        "lm_backend",
        "task_type",
        "use_cot_caption",
        "use_cot_language",
        "constrained_decoding",
        "repainting_start",
        "repainting_end",
        "instruction",
    }
    return {key: value for key, value in frozen.items() if key in allowed}


def _append_request(
    project: dict[str, Any],
    frozen: dict[str, Any],
    *,
    adapter_info: dict[str, Any],
) -> dict[str, Any]:
    request = {
        "requestId": new_id("request"),
        "fingerprint": fingerprint(
            {
                "adapterVersion": "ace-rest-v1",
                "adapterInfo": adapter_info,
                "parameters": frozen,
            }
        ),
        "adapter": "ace-step-rest",
        "adapterVersion": "ace-rest-v1",
        "adapterInfo": deepcopy(adapter_info),
        "parameters": deepcopy(frozen),
        "createdAt": utc_now(),
    }
    project["requests"].append(request)
    return request


def _find_reusable_candidate(
    store: ProjectStore, project: dict[str, Any], request_fingerprint: str
) -> dict[str, Any] | None:
    request_ids = {
        record["requestId"]
        for record in project["requests"]
        if record.get("fingerprint") == request_fingerprint
    }
    for candidate in reversed(project["candidates"]):
        if candidate["requestId"] not in request_ids or candidate["status"] != "ready":
            continue
        artifact = store.find_by_id(
            project, "artifacts", "artifactId", candidate["artifactId"]
        )
        valid, _ = store.verify_artifact(artifact)
        if valid:
            return candidate
    return None


def _record_generated_candidate(
    store: ProjectStore,
    project: dict[str, Any],
    *,
    request: dict[str, Any],
    job: dict[str, Any],
    destination: Path,
    parent_candidate_id: str | None,
    edit_range: dict[str, float] | None,
    context_range: dict[str, float] | None,
) -> dict[str, Any]:
    artifact, findings = artifact_and_findings(
        path=destination,
        project_relative_path=store.relative_path(destination),
        artifact_kind="candidate-audio",
        created_by_job_id=job["jobId"],
        requested_duration_seconds=float(request["parameters"]["audio_duration"]),
    )
    project["artifacts"].append(artifact)
    project["findings"].extend(findings)
    candidate = {
        "candidateId": new_id("candidate"),
        "requestId": request["requestId"],
        "artifactId": artifact["artifactId"],
        "findingIds": [finding["findingId"] for finding in findings],
        "status": "ready",
        "parentCandidateId": parent_candidate_id,
        "editRange": deepcopy(edit_range),
        "contextRange": deepcopy(context_range),
        "humanReview": {
            "status": "unreviewed",
            "rating": None,
            "notes": [],
            "updatedAt": None,
        },
        "createdAt": utc_now(),
    }
    project["candidates"].append(candidate)
    job["resultRefs"] = [candidate["candidateId"], artifact["artifactId"]]
    return candidate


def generate_candidates(
    project_root: Path | str,
    *,
    seeds: Iterable[int],
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_DIT_MODEL,
    lm_model: str = DEFAULT_LM_MODEL,
    poll_seconds: float = 1.0,
    timeout_seconds: float = 1800.0,
    explicit_resume: bool = False,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
) -> dict[str, Any]:
    store = ProjectStore(project_root)
    seed_list = [int(seed) for seed in seeds]
    if not seed_list:
        raise ValueError("at least one seed is required")
    with store.locked():
        project = store.load()
        client = client_factory(base_url)
        adapter_info = client.health()
        batch = store.append_job(
            project,
            kind="candidate-batch",
            parameters={
                "seeds": seed_list,
                "model": model,
                "lmModel": lm_model,
                "baseUrl": base_url,
                "explicitResume": explicit_resume,
            },
        )
        store.transition_job(batch, "running", stage="preparing", progress=0.0)
        store.save(project)

        completed: list[str] = []
        reused: list[str] = []
        failures: list[dict[str, str]] = []
        try:
            for index, seed in enumerate(seed_list):
                frozen = _frozen_generation_payload(
                    project, seed=seed, model=model, lm_model=lm_model
                )
                prospective_fingerprint = fingerprint(
                    {
                        "adapterVersion": "ace-rest-v1",
                        "adapterInfo": adapter_info,
                        "parameters": frozen,
                    }
                )
                if explicit_resume:
                    reusable = _find_reusable_candidate(
                        store, project, prospective_fingerprint
                    )
                    if reusable is not None:
                        reused.append(reusable["candidateId"])
                        completed.append(reusable["candidateId"])
                        batch["progress"] = (index + 1) / len(seed_list)
                        batch["stage"] = f"reused seed {seed}"
                        store.save(project)
                        continue

                request = _append_request(
                    project, frozen, adapter_info=adapter_info
                )
                job = store.append_job(
                    project,
                    kind="generate-candidate",
                    parameters={"requestId": request["requestId"], "seed": seed},
                    parent_job_id=batch["jobId"],
                )
                store.transition_job(job, "running", stage="submitting", progress=0.0)
                store.save(project)
                try:
                    task_id = client.submit(_api_payload(frozen))
                    job["remoteTaskId"] = task_id

                    def progress(state: dict[str, Any]) -> None:
                        job["stage"] = str(state["stage"])
                        job["progress"] = float(state["progress"])
                        batch["stage"] = f"seed {seed}: {job['stage']}"
                        batch["progress"] = (index + job["progress"]) / len(seed_list)
                        store.save(project)

                    result = client.wait(
                        task_id,
                        poll_seconds=poll_seconds,
                        timeout_seconds=timeout_seconds,
                        on_progress=progress,
                    )
                    file_path = str(result.get("file") or "")
                    if not file_path:
                        raise RuntimeError("ACE task succeeded without an audio file")
                    destination = (
                        store.root
                        / "artifacts"
                        / "candidates"
                        / f"{job['jobId']}-seed-{seed}.wav"
                    )
                    client.download(file_path, destination)
                    candidate = _record_generated_candidate(
                        store,
                        project,
                        request=request,
                        job=job,
                        destination=destination,
                        parent_candidate_id=None,
                        edit_range=None,
                        context_range=None,
                    )
                    job["remoteResult"] = {
                        key: result.get(key)
                        for key in (
                            "generation_info",
                            "seed_value",
                            "lm_model",
                            "dit_model",
                            "metas",
                        )
                    }
                    store.transition_job(job, "succeeded", stage="verified", progress=1.0)
                    completed.append(candidate["candidateId"])
                except Exception as error:
                    store.transition_job(
                        job, "failed", stage="failed", error=f"{type(error).__name__}: {error}"
                    )
                    failures.append({"seed": str(seed), "error": job["error"]})
                batch["progress"] = (index + 1) / len(seed_list)
                store.save(project)
        except KeyboardInterrupt:
            batch["cancelRequested"] = True
            store.transition_job(batch, "cancelled", stage="cancelled", error="KeyboardInterrupt")
            store.save(project)
            raise

        if failures and completed:
            store.transition_job(batch, "partial", stage="partial", progress=1.0)
        elif failures:
            store.transition_job(batch, "failed", stage="failed", progress=1.0)
        else:
            store.transition_job(batch, "succeeded", stage="verified", progress=1.0)
        batch["resultRefs"] = completed
        batch["failures"] = failures
        batch["reusedCandidateIds"] = reused
        store.save(project)
        return {
            "batchJobId": batch["jobId"],
            "status": batch["status"],
            "candidateIds": completed,
            "reusedCandidateIds": reused,
            "failures": failures,
        }


def resume_latest_batch(
    project_root: Path | str,
    *,
    base_url: str | None = None,
    poll_seconds: float = 1.0,
    timeout_seconds: float = 1800.0,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
) -> dict[str, Any]:
    store = ProjectStore(project_root)
    project = store.load()
    previous = next(
        (job for job in reversed(project["jobs"]) if job["kind"] == "candidate-batch"),
        None,
    )
    if previous is None:
        raise ValueError("no candidate batch exists to resume")
    parameters = previous["parameters"]
    return generate_candidates(
        project_root,
        seeds=parameters["seeds"],
        base_url=base_url or parameters["baseUrl"],
        model=parameters["model"],
        lm_model=parameters["lmModel"],
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        explicit_resume=True,
        client_factory=client_factory,
    )


def repaint_candidate(
    project_root: Path | str,
    *,
    start_seconds: float,
    end_seconds: float,
    instruction: str,
    seed: int,
    candidate_id: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_DIT_MODEL,
    lm_model: str = DEFAULT_LM_MODEL,
    poll_seconds: float = 1.0,
    timeout_seconds: float = 1800.0,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
) -> dict[str, Any]:
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError("repaint range must satisfy 0 <= start < end")
    store = ProjectStore(project_root)
    with store.locked():
        project = store.load()
        parent_id = candidate_id or project.get("selectedCandidateId")
        if not parent_id:
            raise ValueError("select a candidate or pass --candidate-id")
        parent = store.find_by_id(project, "candidates", "candidateId", parent_id)
        parent_artifact = store.find_by_id(
            project, "artifacts", "artifactId", parent["artifactId"]
        )
        valid, reason = store.verify_artifact(parent_artifact)
        if not valid:
            raise ValueError(reason)
        source = store.resolve_artifact(parent_artifact)
        duration = inspect_wav(source)["durationSeconds"]
        if end_seconds > duration:
            raise ValueError(f"repaint end exceeds audio duration {duration:.3f}s")
        client = client_factory(base_url)
        adapter_info = client.health()
        edit_range = {
            "startSeconds": float(start_seconds),
            "endSeconds": float(end_seconds),
        }
        frozen = _frozen_generation_payload(
            project,
            seed=seed,
            model=model,
            lm_model=lm_model,
            task_type="repaint",
            parent_artifact_sha256=parent_artifact["sha256"],
            edit_range=edit_range,
            instruction=instruction,
        )
        request = _append_request(project, frozen, adapter_info=adapter_info)
        job = store.append_job(
            project,
            kind="repaint-candidate",
            parameters={"requestId": request["requestId"], "parentCandidateId": parent_id},
        )
        store.transition_job(job, "running", stage="submitting", progress=0.0)
        store.save(project)
        try:
            task_id = client.submit(_api_payload(frozen), source_audio=source)
            job["remoteTaskId"] = task_id

            def progress(state: dict[str, Any]) -> None:
                job["stage"] = str(state["stage"])
                job["progress"] = float(state["progress"])
                store.save(project)

            result = client.wait(
                task_id,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
                on_progress=progress,
            )
            file_path = str(result.get("file") or "")
            if not file_path:
                raise RuntimeError("ACE repaint succeeded without an audio file")
            destination = (
                store.root
                / "artifacts"
                / "repaints"
                / f"{job['jobId']}-seed-{seed}.wav"
            )
            client.download(file_path, destination)
            candidate = _record_generated_candidate(
                store,
                project,
                request=request,
                job=job,
                destination=destination,
                parent_candidate_id=parent_id,
                edit_range=edit_range,
                context_range={"startSeconds": 0.0, "endSeconds": duration},
            )
            store.transition_job(job, "succeeded", stage="verified", progress=1.0)
            job["remoteResult"] = {
                key: result.get(key)
                for key in ("generation_info", "seed_value", "lm_model", "dit_model", "metas")
            }
            store.save(project)
            return {"jobId": job["jobId"], "candidateId": candidate["candidateId"]}
        except Exception as error:
            store.transition_job(
                job, "failed", stage="failed", error=f"{type(error).__name__}: {error}"
            )
            store.save(project)
            raise


def select_candidate(project_root: Path | str, candidate_id: str) -> dict[str, Any]:
    store = ProjectStore(project_root)
    with store.locked():
        project = store.load()
        candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
        artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
        valid, reason = store.verify_artifact(artifact)
        if not valid:
            raise ValueError(reason)
        previous = project.get("selectedCandidateId")
        project["selectedCandidateId"] = candidate_id
        revision = {
            "revisionId": new_id("revision"),
            "kind": "candidate-selection",
            "previousRevisionId": (
                project["revisions"][-1]["revisionId"] if project["revisions"] else None
            ),
            "before": {"selectedCandidateId": previous},
            "after": {"selectedCandidateId": candidate_id},
            "createdAt": utc_now(),
        }
        project["revisions"].append(revision)
        store.save(project)
        return revision


def undo_selection(project_root: Path | str) -> dict[str, Any]:
    store = ProjectStore(project_root)
    with store.locked():
        project = store.load()
        selection = next(
            (
                revision
                for revision in reversed(project["revisions"])
                if revision["kind"] == "candidate-selection"
                and not revision.get("undoneByRevisionId")
            ),
            None,
        )
        if selection is None:
            raise ValueError("no candidate selection can be undone")
        current = project.get("selectedCandidateId")
        restored = selection["before"]["selectedCandidateId"]
        project["selectedCandidateId"] = restored
        revision = {
            "revisionId": new_id("revision"),
            "kind": "selection-undo",
            "previousRevisionId": (
                project["revisions"][-1]["revisionId"] if project["revisions"] else None
            ),
            "undoesRevisionId": selection["revisionId"],
            "before": {"selectedCandidateId": current},
            "after": {"selectedCandidateId": restored},
            "createdAt": utc_now(),
        }
        selection["undoneByRevisionId"] = revision["revisionId"]
        project["revisions"].append(revision)
        store.save(project)
        return revision


def review_candidate(
    project_root: Path | str,
    candidate_id: str,
    *,
    status: str,
    note: str | None = None,
    rating: int | None = None,
) -> dict[str, Any]:
    allowed = {"unreviewed", "listened", "approved", "rejected"}
    if status not in allowed:
        raise ValueError(f"review status must be one of: {', '.join(sorted(allowed))}")
    if rating is not None and rating not in range(1, 6):
        raise ValueError("rating must be between 1 and 5")
    store = ProjectStore(project_root)
    with store.locked():
        project = store.load()
        candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
        review = candidate["humanReview"]
        review["status"] = status
        if status == "unreviewed":
            review["rating"] = None
        elif rating is not None:
            review["rating"] = rating
        if note:
            review["notes"].append({"text": note, "createdAt": utc_now()})
        review["updatedAt"] = utc_now()
        store.save(project)
        return deepcopy(review)


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def export_selected(
    project_root: Path | str, *, output: Path | str | None = None
) -> dict[str, Any]:
    store = ProjectStore(project_root)
    with store.locked():
        project = store.load()
        selected_id = project.get("selectedCandidateId")
        if not selected_id:
            raise ValueError("no candidate is selected")
        candidate = store.find_by_id(project, "candidates", "candidateId", selected_id)
        source_artifact = store.find_by_id(
            project, "artifacts", "artifactId", candidate["artifactId"]
        )
        valid, reason = store.verify_artifact(source_artifact)
        if not valid:
            raise ValueError(reason)
        source = store.resolve_artifact(source_artifact)
        job = store.append_job(
            project,
            kind="export",
            parameters={"candidateId": selected_id, "requestedOutput": str(output or "")},
        )
        store.transition_job(job, "running", stage="copying", progress=0.1)
        store.save(project)
        internal = store.root / "exports" / f"{job['jobId']}-{selected_id}.wav"
        try:
            _copy_atomic(source, internal)
            if sha256_file(internal) != source_artifact["sha256"]:
                raise RuntimeError("export hash differs from the selected candidate")
            artifact, findings = artifact_and_findings(
                path=internal,
                project_relative_path=store.relative_path(internal),
                artifact_kind="export-wav",
                created_by_job_id=job["jobId"],
                requested_duration_seconds=source_artifact["audio"]["durationSeconds"],
            )
            if output is not None:
                external = Path(output).expanduser().resolve()
                _copy_atomic(internal, external)
                if sha256_file(external) != artifact["sha256"]:
                    raise RuntimeError("external export hash verification failed")
                artifact["externalPath"] = str(external)
            project["artifacts"].append(artifact)
            project["findings"].extend(findings)
            job["resultRefs"] = [artifact["artifactId"]]
            revision = {
                "revisionId": new_id("revision"),
                "kind": "export",
                "previousRevisionId": (
                    project["revisions"][-1]["revisionId"] if project["revisions"] else None
                ),
                "before": {},
                "after": {
                    "candidateId": selected_id,
                    "exportArtifactId": artifact["artifactId"],
                },
                "createdAt": utc_now(),
            }
            project["revisions"].append(revision)
            store.transition_job(job, "succeeded", stage="verified", progress=1.0)
            store.save(project)
            return {
                "artifactId": artifact["artifactId"],
                "path": str(internal),
                "externalPath": artifact.get("externalPath"),
                "sha256": artifact["sha256"],
            }
        except Exception as error:
            store.transition_job(
                job, "failed", stage="failed", error=f"{type(error).__name__}: {error}"
            )
            store.save(project)
            raise
