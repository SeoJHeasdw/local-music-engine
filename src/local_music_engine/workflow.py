"""Project workflows shared by the CLI and the future app server."""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterable

from .ace_adapter import AceApiError, AceStepClient
from .execution import execute_candidate
from .jobs import (ACTIVE_STATUSES, frozen_batch_payloads, recover_jobs, reusable_candidates, resume_source)
from .qc import artifact_and_findings, inspect_wav
from .storage import ProjectStore, fingerprint, new_id, sha256_file, utc_now

DEFAULT_BASE_URL = "http://127.0.0.1:18001"
DEFAULT_DIT_MODEL = "acestep-v15-turbo"
DEFAULT_LM_MODEL = "acestep-5Hz-lm-4B"
# ACE's server default is 0.85. On the same six seeds of a Korean/English song
# (turbo + 4B LM, 2026-09-24), 0.6 lowered the ASR-measured Korean CER from 0.35 to
# 0.19 and English WER from 0.45 to 0.32. ASR is a proxy, not a listening verdict.
DEFAULT_LM_TEMPERATURE = 0.6

# ACE's balanced repaint mode takes 0 (keep the source) to 1 (pure diffusion).
REPAINT_STRENGTHS = {"light": 0.25, "medium": 0.5, "strong": 0.8}
INSTRUMENTAL_LYRICS = "[Instrumental]"
# ACE reads these optional musical metas; the project stores them under its own names.
_META_FIELDS = (("bpm", "bpm"), ("keyScale", "key_scale"), ("timeSignature", "time_signature"))
_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")
_SECTION_TAG = re.compile(r"\s*\[[^\]]*\]\s*")


def _dit_sampling(model: str) -> dict[str, Any]:
    """Step count and guidance that fit the DiT checkpoint.

    Turbo checkpoints are distilled for 8 steps and ignore CFG. SFT/base checkpoints
    are trained for the full schedule with classifier-free guidance (ACE Tutorial:
    50 steps, CFG around 7); run at 8 steps they are simply under-denoised.
    """

    if "turbo" in model:
        return {"inference_steps": 8}
    return {"inference_steps": 50, "guidance_scale": 7.0}


def _vocal_language(lyrics: str) -> str:
    """ACE's lyric header takes one language. Korean leads whenever Hangul is sung,
    including bilingual lyrics; English-only lyrics are not labelled Korean."""

    sung = "\n".join(line for line in lyrics.splitlines() if not _SECTION_TAG.fullmatch(line))
    if _HANGUL.search(sung):
        return "ko"
    if _LATIN.search(sung):
        return "en"
    return "ko"


def _require_loaded_models(adapter_info: dict[str, Any], frozen: dict[str, Any]) -> None:
    """ACE loads one DiT and one LM at start and silently serves any other requested
    name with them. Refuse, so project.json never records a model that did not run."""

    loaded = adapter_info.get("loaded_model")
    if loaded and loaded != frozen["model"]:
        raise AceApiError(
            f"ACE server has DiT {loaded} loaded, not the requested {frozen['model']}; "
            "restart the engine with that model (MUSIC_ENGINE_ACE_DIT_MODEL) or request the loaded one"
        )
    loaded_lm = adapter_info.get("loaded_lm_model")
    if frozen.get("thinking") and loaded_lm and loaded_lm != frozen["lm_model_path"]:
        raise AceApiError(
            f"ACE server has LM {loaded_lm} loaded, not the requested {frozen['lm_model_path']}; "
            "restart the engine with that model (MUSIC_ENGINE_ACE_LM_MODEL) or request the loaded one"
        )


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
    style_prompt: str | None = None,
    repaint_strength: float | None = None,
    lm_temperature: float = DEFAULT_LM_TEMPERATURE,
) -> dict[str, Any]:
    inputs = project["inputs"]
    payload: dict[str, Any] = {
        "prompt": style_prompt if style_prompt is not None else inputs["stylePrompt"],
        "lyrics": inputs["lyricsNormalized"],
        "thinking": task_type == "text2music",
        "vocal_language": _vocal_language(inputs["lyricsNormalized"]),
        "audio_format": "wav",
        "audio_duration": float(inputs["targetDurationSeconds"]),
        **_dit_sampling(model),
        "use_random_seed": False,
        "seed": int(seed),
        "batch_size": 1,
        "model": model,
        "lm_model_path": lm_model,
        "lm_backend": "mlx",
        "task_type": task_type,
        "project_structure": inputs.get("structure"),
    }
    # Only present metas enter the payload so fingerprints of older projects stay stable.
    for source, target in _META_FIELDS:
        value = inputs.get(source)
        if value not in (None, ""):
            payload[target] = value
    if task_type == "text2music":
        payload.update(
            {
                "use_cot_caption": False,
                "use_cot_language": False,
                "constrained_decoding": True,
                # The LM plans melody and phrasing only for text2music; repaint skips it.
                "lm_temperature": float(lm_temperature),
            }
        )
    if edit_range is not None:
        payload["repainting_start"] = edit_range["startSeconds"]
        payload["repainting_end"] = edit_range["endSeconds"]
    if repaint_strength is not None:
        payload["repaint_mode"] = "balanced"
        payload["repaint_strength"] = float(repaint_strength)
    if instruction:
        # ACE treats this as the DiT task template, not a free-form request. Steering
        # belongs in the caption; this override exists only for deliberate experiments.
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
        "guidance_scale",
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
        "lm_temperature",
        "repainting_start",
        "repainting_end",
        "repaint_mode",
        "repaint_strength",
        "instruction",
        "bpm",
        "key_scale",
        "time_signature",
    }
    return {key: value for key, value in frozen.items() if key in allowed}


def _revise_inputs(
    project: dict[str, Any],
    *,
    title: str | None = None,
    style_prompt: str | None = None,
    lyrics: str | None = None,
    duration_seconds: float | None = None,
    bpm: int | None = None,
    key_scale: str | None = None,
    time_signature: str | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Change project inputs in place and record the before/after as a revision.

    Past requests froze their own parameters, so earlier candidates keep their
    provenance. ``bpm=0`` and empty key/time strings clear the stored meta.
    """

    inputs = project["inputs"]
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    def change(key: str, value: Any) -> None:
        if inputs.get(key) != value:
            before[key] = inputs.get(key)
            after[key] = value
            inputs[key] = value

    if title is not None:
        cleaned = title.strip() or "Untitled"
        if project["title"] != cleaned:
            before["title"] = project["title"]
            after["title"] = cleaned
            project["title"] = cleaned
    if style_prompt is not None:
        if not style_prompt.strip():
            raise ValueError("style prompt must not be empty")
        change("stylePrompt", style_prompt.strip())
    if lyrics is not None:
        if not lyrics.strip():
            raise ValueError("lyrics must not be empty; use [Instrumental] for no vocals")
        change("lyricsOriginal", lyrics)
        change("lyricsNormalized", lyrics.replace("\r\n", "\n").replace("\r", "\n"))
    if duration_seconds is not None:
        if not math.isfinite(duration_seconds) or duration_seconds < 10 or duration_seconds > 600:
            raise ValueError("duration must be between 10 and 600 seconds")
        change("targetDurationSeconds", float(duration_seconds))
    if bpm is not None:
        if bpm and not 30 <= bpm <= 300:
            raise ValueError("bpm must be between 30 and 300")
        change("bpm", bpm or None)
    if key_scale is not None:
        change("keyScale", key_scale.strip() or None)
    if time_signature is not None:
        change("timeSignature", time_signature.strip() or None)
    if not after:
        return None
    revision = {
        "revisionId": new_id("revision"),
        "kind": "inputs-change",
        "previousRevisionId": (
            project["revisions"][-1]["revisionId"] if project["revisions"] else None
        ),
        "before": before,
        "after": after,
        "reason": reason,
        "createdAt": utc_now(),
    }
    project["revisions"].append(revision)
    return revision


def _append_feedback(
    project: dict[str, Any], feedback: dict[str, Any], *, job_id: str
) -> dict[str, Any]:
    """Keep the listener's own words next to the plan that was actually executed."""

    text = str(feedback.get("text") or "").strip()
    edit_range = feedback.get("range")
    if edit_range is not None:
        edit_range = {
            "startSeconds": float(edit_range["startSeconds"]),
            "endSeconds": float(edit_range["endSeconds"]),
        }
    record = {
        "feedbackId": new_id("feedback"),
        "candidateId": feedback.get("candidateId"),
        "text": text[:2000],
        "range": edit_range,
        "plan": deepcopy(feedback.get("plan")),
        "jobId": job_id,
        "createdAt": utc_now(),
    }
    project.setdefault("feedback", []).append(record)
    return record


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


def _run_batch(
    store: ProjectStore,
    *,
    batch_id: str,
    payloads: list[dict[str, Any]],
    reusable: dict[int, str],
    client: AceStepClient,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Caller owns the generation lease. Every write reloads the current manifest."""

    adapter_info = None
    try:
        for index, frozen in enumerate(payloads):
            seed = frozen["seed"]
            if seed in reusable:
                with store.transaction() as project:
                    batch = store.find_by_id(project, "jobs", "jobId", batch_id)
                    candidate_id = reusable[seed]
                    batch["resultRefs"].append(candidate_id)
                    batch["reusedCandidateIds"].append(candidate_id)
                    batch.update(progress=(index + 1) / len(payloads), stage=f"reused seed {seed}")
                continue
            if adapter_info is None:
                adapter_info = client.health()
            _require_loaded_models(adapter_info, frozen)
            with store.transaction() as project:
                request = _append_request(project, frozen, adapter_info=adapter_info)
                job = store.append_job(
                    project, kind="generate-candidate",
                    parameters={"requestId": request["requestId"], "seed": seed},
                    parent_job_id=batch_id,
                )
                store.transition_job(job, "running", stage="submitting")
            try:
                execute_candidate(
                    store, client, request=request, job_id=job["jobId"],
                    api_payload=_api_payload(frozen), batch_id=batch_id,
                    index=index, total=len(payloads), poll_seconds=poll_seconds,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as error:
                with store.transaction() as project:
                    batch = store.find_by_id(project, "jobs", "jobId", batch_id)
                    batch["failures"].append({"seed": str(seed), "error": f"{type(error).__name__}: {error}"})
                    batch["progress"] = (index + 1) / len(payloads)
        with store.transaction() as project:
            batch = store.find_by_id(project, "jobs", "jobId", batch_id)
            state = "partial" if batch["failures"] and batch["resultRefs"] else "failed" if batch["failures"] else "succeeded"
            store.transition_job(batch, state, stage="verified" if state == "succeeded" else state, progress=1.0)
    except (Exception, KeyboardInterrupt) as error:
        with store.transaction() as project:
            batch = store.find_by_id(project, "jobs", "jobId", batch_id)
            if batch["status"] in ACTIVE_STATUSES:
                cancelled = isinstance(error, KeyboardInterrupt)
                batch["cancelRequested"] = cancelled
                state = "cancelled" if cancelled else "failed"
                store.transition_job(batch, state, stage=state, error=f"{type(error).__name__}: {error}")
        raise
    return {
        "batchJobId": batch_id,
        "status": batch["status"],
        "candidateIds": batch["resultRefs"],
        "newCandidateIds": [item for item in batch["resultRefs"] if item not in batch["reusedCandidateIds"]],
        "reusedCandidateIds": batch["reusedCandidateIds"],
        "failures": batch["failures"],
    }


def _new_batch(store: ProjectStore, project: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    batch = store.append_job(project, kind="candidate-batch", parameters=parameters)
    batch.update(failures=[], reusedCandidateIds=[])
    store.transition_job(batch, "running", stage="preparing")
    return batch


def generate_candidates(
    project_root: Path | str,
    *,
    seeds: Iterable[int],
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_DIT_MODEL,
    lm_model: str = DEFAULT_LM_MODEL,
    lm_temperature: float = DEFAULT_LM_TEMPERATURE,
    poll_seconds: float = 1.0,
    timeout_seconds: float = 1800.0,
    style_prompt: str | None = None,
    lyrics: str | None = None,
    bpm: int | None = None,
    feedback: dict[str, Any] | None = None,
    source_candidate_id: str | None = None,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
) -> dict[str, Any]:
    store = ProjectStore(project_root)
    seed_list = [int(seed) for seed in seeds]
    if not math.isfinite(lm_temperature) or not 0.0 < lm_temperature <= 2.0:
        raise ValueError("lm temperature must be in (0, 2]")
    if not seed_list:
        raise ValueError("at least one seed is required")
    if len(set(seed_list)) != len(seed_list):
        raise ValueError("seeds must be unique within a batch")
    client = client_factory(base_url)
    with store.generation_lock():
        with store.transaction() as project:
            recover_jobs(store, project)
            changes: dict[str, Any] = {}
            if source_candidate_id:
                source = store.find_by_id(project, "candidates", "candidateId", source_candidate_id)
                original = store.find_by_id(project, "requests", "requestId", source["requestId"])["parameters"]
                changes.update(style_prompt=original["prompt"], lyrics=original["lyrics"],
                               duration_seconds=original["audio_duration"], bpm=original.get("bpm") or 0,
                               key_scale=original.get("key_scale") or "", time_signature=original.get("time_signature") or "")
            for field, value in (("style_prompt", style_prompt), ("lyrics", lyrics), ("bpm", bpm)):
                if value is not None:
                    changes[field] = value
            _revise_inputs(project, **changes, reason="feedback" if feedback else "new-batch")
            payloads = [
                _frozen_generation_payload(project, seed=seed, model=model, lm_model=lm_model, lm_temperature=lm_temperature)
                for seed in seed_list
            ]
            batch = _new_batch(store, project, {
                "seeds": seed_list, "model": model, "lmModel": lm_model,
                "baseUrl": base_url, "explicitResume": False, "frozenPayloads": payloads,
                "sourceCandidateId": source_candidate_id,
            })
            if feedback:
                record = _append_feedback(project, feedback, job_id=batch["jobId"])
                batch["parameters"]["feedbackId"] = record["feedbackId"]
        return _run_batch(store, batch_id=batch["jobId"], payloads=payloads, reusable={},
                          client=client, poll_seconds=poll_seconds, timeout_seconds=timeout_seconds)


def resume_latest_batch(
    project_root: Path | str,
    *,
    job_id: str | None = None,
    base_url: str | None = None,
    poll_seconds: float = 1.0,
    timeout_seconds: float = 1800.0,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
) -> dict[str, Any]:
    store = ProjectStore(project_root)
    with store.generation_lock():
        project = store.load()
        previous = resume_source(project, job_id)
        payloads = frozen_batch_payloads(project, previous)
        reusable = reusable_candidates(store, project, previous, payloads)
        parameters = deepcopy(previous["parameters"])
        parameters.update(explicitResume=True, resumeOfJobId=previous["jobId"], frozenPayloads=payloads)
        if base_url is not None:
            parameters["baseUrl"] = base_url
        client = client_factory(parameters["baseUrl"])
        with store.transaction() as project:
            recover_jobs(store, project)
            batch = _new_batch(store, project, parameters)
        return _run_batch(store, batch_id=batch["jobId"], payloads=payloads, reusable=reusable,
                          client=client, poll_seconds=poll_seconds, timeout_seconds=timeout_seconds)


def repaint_candidate(
    project_root: Path | str,
    *,
    start_seconds: float,
    end_seconds: float,
    seed: int,
    instruction: str | None = None,
    style_prompt: str | None = None,
    lyrics: str | None = None,
    strength: str | None = None,
    feedback: dict[str, Any] | None = None,
    candidate_id: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    model: str | None = None,
    lm_model: str | None = None,
    poll_seconds: float = 1.0,
    timeout_seconds: float = 1800.0,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
) -> dict[str, Any]:
    if not math.isfinite(start_seconds) or not math.isfinite(end_seconds) or start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError("repaint range must satisfy finite 0 <= start < end")
    if strength is not None and strength not in REPAINT_STRENGTHS:
        raise ValueError(f"strength must be one of: {', '.join(REPAINT_STRENGTHS)}")
    if style_prompt is not None and not style_prompt.strip():
        raise ValueError("style prompt must not be empty")
    if lyrics is not None and not lyrics.strip():
        raise ValueError("lyrics must not be empty; use [Instrumental] for no vocals")
    store = ProjectStore(project_root)
    client = client_factory(base_url)
    with store.generation_lock():
        project = store.load()
        parent_id = candidate_id or project.get("selectedCandidateId")
        if not parent_id:
            raise ValueError("select a candidate or pass --candidate-id")
        parent = store.find_by_id(project, "candidates", "candidateId", parent_id)
        parent_artifact = store.find_by_id(project, "artifacts", "artifactId", parent["artifactId"])
        valid, reason = store.verify_artifact(parent_artifact)
        if not valid:
            raise ValueError(reason)
        source = store.resolve_artifact(parent_artifact)
        duration = inspect_wav(source)["durationSeconds"]
        if end_seconds > duration:
            raise ValueError(f"repaint end exceeds audio duration {duration:.3f}s")
        # Editing an old version follows that version's lyrics/style/metas. Project
        # defaults may have changed since then and must not leak into this revision.
        original = store.find_by_id(project, "requests", "requestId", parent["requestId"])["parameters"]
        source_inputs = {
            "stylePrompt": original["prompt"], "lyricsNormalized": original["lyrics"],
            "targetDurationSeconds": duration, "structure": original.get("project_structure"),
        }
        for field, api_field in _META_FIELDS:
            source_inputs[field] = original.get(api_field)
        if lyrics is not None:
            source_inputs["lyricsNormalized"] = lyrics.replace("\r\n", "\n").replace("\r", "\n")
        edit_range = {"startSeconds": float(start_seconds), "endSeconds": float(end_seconds)}
        frozen = _frozen_generation_payload(
            {"inputs": source_inputs}, seed=seed,
            model=model or original.get("model", DEFAULT_DIT_MODEL),
            lm_model=lm_model or original.get("lm_model_path", DEFAULT_LM_MODEL),
            task_type="repaint", parent_artifact_sha256=parent_artifact["sha256"],
            edit_range=edit_range, instruction=instruction,
            style_prompt=style_prompt.strip() if style_prompt is not None else None,
            repaint_strength=REPAINT_STRENGTHS[strength] if strength else None,
        )
        adapter_info = client.health()
        _require_loaded_models(adapter_info, frozen)
        with store.transaction() as project:
            recover_jobs(store, project)
            request = _append_request(project, frozen, adapter_info=adapter_info)
            job = store.append_job(project, kind="repaint-candidate", parameters={
                "requestId": request["requestId"], "parentCandidateId": parent_id, "strength": strength,
            })
            if feedback:
                record = _append_feedback(project, feedback, job_id=job["jobId"])
                job["parameters"]["feedbackId"] = record["feedbackId"]
            store.transition_job(job, "running", stage="submitting")
        candidate_id = execute_candidate(
            store, client, request=request, job_id=job["jobId"], api_payload=_api_payload(frozen),
            poll_seconds=poll_seconds, timeout_seconds=timeout_seconds, source=source,
            parent_candidate_id=parent_id, edit_range=edit_range,
            context_range={"startSeconds": 0.0, "endSeconds": duration},
        )
        return {"jobId": job["jobId"], "candidateId": candidate_id}


def revise_inputs(project_root: Path | str, **changes: Any) -> dict[str, Any] | None:
    """Edit title/style/lyrics/duration/metas for future requests; history is kept."""

    store = ProjectStore(project_root)
    with store.locked():
        project = store.load()
        revision = _revise_inputs(project, **changes)
        if revision is not None:
            store.save(project)
        return revision


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
        if restored is not None:
            candidate = store.find_by_id(project, "candidates", "candidateId", restored)
            artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
            valid, reason = store.verify_artifact(artifact)
            if not valid:
                raise ValueError(reason)
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
        # Atomic publish without replacing an existing artifact, even if another
        # exporter won the filename between validation and this copy.
        os.link(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
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
        external = None
        if output is not None:
            requested = Path(output).expanduser().absolute()
            external = requested.resolve()
            if external.is_relative_to(store.root):
                raise ValueError("export output must be outside the project; omit --output for an internal export")
            if requested.exists() or requested.is_symlink():
                raise FileExistsError("export output already exists; choose a new filename")
        job = store.append_job(
            project,
            kind="export",
            parameters={"candidateId": selected_id, "requestedOutput": str(output or "")},
        )
        store.transition_job(job, "running", stage="copying", progress=0.1)
        store.save(project)
        internal = store.root / "exports" / f"{job['jobId']}-{selected_id}.wav"
        try:
            store.relative_path(internal)
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
            if external is not None:
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
