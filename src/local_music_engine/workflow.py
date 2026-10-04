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
from .jobs import (ACTIVE_STATUSES, cleanup_export_outputs, frozen_batch_payloads, recover_export_jobs, recover_jobs,
                   reusable_candidates, resume_source, validate_seed)
from .qc import artifact_and_findings, inspect_wav
from .storage import ProjectStore, fingerprint, new_id, sha256_file, utc_now
from .auto_quality import finish_single_edit, make_quality_plan, run_quality_batch, validate_quality_plan
from .production_rules import editable_style, freeze_guidance, guided_prompt, normalize_selection, normalize_time_signature
from .song_planning import prepare_song_plan
from .music3 import BASE_URL, CAPABILITIES, ENGINE, MAX_DURATION_SECONDS, MODEL, translate_payload
from .music3_adapter import Music3Client
from .lyrics import INSTRUMENTAL_LYRICS, is_instrumental_lyrics

DEFAULT_BASE_URL = BASE_URL
DEFAULT_ENGINE = ENGINE
DEFAULT_DIT_MODEL = MODEL
LEGACY_ENGINE = "ace-step"
LEGACY_BASE_URL = "http://127.0.0.1:18001"
LEGACY_DIT_MODEL = "acestep-v15-turbo"
DEFAULT_LM_MODEL = "acestep-5Hz-lm-4B"
# ACE's server default is 0.85. On the same six seeds of a Korean/English song
# (turbo + 4B LM, 2026-09-24), 0.6 lowered the ASR-measured Korean CER from 0.35 to
# 0.19 and English WER from 0.45 to 0.32. ASR is a proxy, not a listening verdict.
DEFAULT_LM_TEMPERATURE = 0.6

# ACE's balanced repaint mode takes 0 (keep the source) to 1 (pure diffusion).
REPAINT_STRENGTHS = {"light": 0.25, "medium": 0.5, "strong": 0.8}
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

    if frozen.get("engine") == ENGINE:
        if (adapter_info.get("engine") != ENGINE or adapter_info.get("status") != "ok"
                or not adapter_info.get("models_initialized") or adapter_info.get("loaded_model") != frozen["model"]):
            raise AceApiError(f"Music 3 server is not ready with the requested model {frozen['model']}; engine/model health must match")
        return
    if adapter_info.get("engine") == ENGINE:
        raise AceApiError("a frozen ACE request cannot run on Music 3; use the explicit legacy ACE engine")
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
    production_rules_snapshot: dict[str, Any] | None = None,
    engine: str | None = None,
) -> dict[str, Any]:
    engine = engine or (LEGACY_ENGINE if model.startswith("acestep-") else ENGINE)
    if engine not in {ENGINE, LEGACY_ENGINE}:
        raise ValueError("unknown music engine")
    if engine == ENGINE and (task_type != "text2music" or parent_artifact_sha256 or edit_range):
        raise ValueError("Music 3 supports text-to-music only; cover, repaint and reference audio are unavailable")
    if engine == ENGINE and model.startswith("acestep-"):
        raise ValueError("ACE models require engine='ace-step'; a Music 3 request cannot use an ACE model")
    if engine == LEGACY_ENGINE and not model.startswith("acestep-"):
        raise ValueError("the legacy ACE engine requires an ACE model")
    inputs = project["inputs"]
    if engine == ENGINE and not 10 <= float(inputs["targetDurationSeconds"]) <= MAX_DURATION_SECONDS:
        raise ValueError("Music 3 duration must be between 10 and 300 seconds")
    base_style = style_prompt if style_prompt is not None else inputs["stylePrompt"]
    selection = inputs.get("productionRules")
    snapshot = None
    if selection is not None or production_rules_snapshot is not None:
        snapshot = freeze_guidance(
            selection or production_rules_snapshot["selection"], base_style=base_style,
            duration_seconds=float(inputs["targetDurationSeconds"]),
            instrumental=is_instrumental_lyrics(inputs["lyricsNormalized"]),
            input_metas={source: inputs.get(source) for source, _ in _META_FIELDS},
            previous_snapshot=production_rules_snapshot,
        )
    payload: dict[str, Any] = {
        "prompt": guided_prompt(base_style, snapshot) if snapshot else base_style,
        "lyrics": inputs["lyricsNormalized"],
        "thinking": task_type == "text2music",
        "vocal_language": _vocal_language(inputs["lyricsNormalized"]),
        "audio_format": "wav",
        "audio_duration": float(inputs["targetDurationSeconds"]),
        **_dit_sampling(model),
        "use_random_seed": False,
        "seed": validate_seed(seed),
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
        if value in (None, "") and snapshot and snapshot["preset"]:
            value = snapshot["preset"].get(source)
        if value not in (None, ""):
            payload[target] = normalize_time_signature(value) if source == "timeSignature" else value
    if snapshot is not None:
        payload["productionRules"] = snapshot
        selected = set(snapshot["appliedRuleIds"])
        if selected.intersection({"section-development", "phrase-breathing"}):
            song_plan = prepare_song_plan(
                payload["lyrics"], duration_seconds=payload["audio_duration"],
                bpm=payload.get("bpm"), time_signature=payload.get("time_signature"),
                preset_id=snapshot["selection"]["presetId"],
                instrumental=is_instrumental_lyrics(inputs["lyricsNormalized"]),
                development="section-development" in selected,
                breathing="phrase-breathing" in selected,
            )
            payload["lyrics"] = song_plan["lyricsPrepared"]
            payload["bpm"] = song_plan["timing"]["bpm"]
            payload["time_signature"] = song_plan["timing"]["timeSignature"]
            payload["songPlan"] = song_plan
            snapshot["songPlan"] = deepcopy(song_plan)
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
    else:
        # The pinned API may reload its planner for caption/language CoT even with
        # thinking=false. Source-conditioned tasks do not need that model.
        payload.update(use_cot_caption=False, use_cot_language=False)
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
    if engine == ENGINE:
        payload["sourceLyricsOriginal"] = inputs["lyricsNormalized"]
        return translate_payload(payload, base_style=base_style)
    return payload


def _api_payload(frozen: dict[str, Any]) -> dict[str, Any]:
    if frozen.get("engine") == ENGINE:
        allowed = {"prompt", "lyrics", "audio_duration", "seed", "model", "batch_size", "inference_steps", "task_type"}
        return {key: value for key, value in frozen.items() if key in allowed}
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
        "audio_cover_strength",
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
    time_signature: str | int | None = None,
    production_rules: dict[str, Any] | None = None,
    production_rules_snapshot: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Change project inputs in place and record the before/after as a revision.

    Past requests froze their own parameters, so earlier candidates keep their
    provenance. ``bpm=0`` and empty key/time strings clear the stored meta.
    """

    inputs = project["inputs"]
    # Validate controls before changing even the in-memory revision or project title.
    selection = (deepcopy(production_rules_snapshot["selection"]) if production_rules_snapshot is not None
                 else normalize_selection(production_rules) if production_rules is not None else inputs.get("productionRules"))
    if time_signature is not None:
        time_signature = normalize_time_signature(time_signature)
        # None is a deliberate clear, while an omitted argument means no change.
        time_signature = time_signature or ""
    if style_prompt is not None and not style_prompt.strip():
        guidance = (freeze_guidance(selection, base_style="", duration_seconds=duration_seconds or inputs["targetDurationSeconds"],
                                   instrumental=is_instrumental_lyrics(lyrics if lyrics is not None else inputs["lyricsNormalized"]),
                                   previous_snapshot=production_rules_snapshot) if selection is not None else None)
        if guidance is None or not guided_prompt("", guidance):
            raise ValueError("style prompt must not be empty without production guidance")
    if production_rules is not None and not (style_prompt if style_prompt is not None else inputs["stylePrompt"]).strip():
        guidance = freeze_guidance(selection, base_style="", duration_seconds=duration_seconds or inputs["targetDurationSeconds"],
                                  instrumental=is_instrumental_lyrics(lyrics if lyrics is not None else inputs["lyricsNormalized"]),
                                  previous_snapshot=production_rules_snapshot)
        if not guided_prompt("", guidance):
            raise ValueError("style prompt must not be empty without production guidance")
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
        change("timeSignature", time_signature or None)
    if production_rules is not None:
        change("productionRules", selection)
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
    music3 = frozen.get("engine") == ENGINE
    adapter = "minimax-music3-mlx" if music3 else "ace-step-rest"
    version = "music3-rest-v1" if music3 else "ace-rest-v1"
    request = {
        "requestId": new_id("request"),
        "fingerprint": fingerprint(
            {
                "adapterVersion": version,
                "adapterInfo": adapter_info,
                "parameters": frozen,
            }
        ),
        "adapter": adapter,
        "adapterVersion": version,
        "adapterInfo": deepcopy(adapter_info),
        "executionKind": "real-inference" if adapter_info.get("realInference") is True else "unverified-transport",
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
    unsubmitted_seeds: list[int] = []
    stop_reason = None
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
                if isinstance(error, TimeoutError):
                    # The server may still own this task. Starting another seed can
                    # queue competing inference while the caller believes it stopped.
                    unsubmitted_seeds = [item["seed"] for item in payloads[index + 1:] if item["seed"] not in reusable]
                    stop_reason = "remote-task-timeout"
                    break
        with store.transaction() as project:
            batch = store.find_by_id(project, "jobs", "jobId", batch_id)
            state = "partial" if batch["failures"] and batch["resultRefs"] else "failed" if batch["failures"] else "succeeded"
            if stop_reason:
                batch.update(stopReason=stop_reason, unsubmittedSeeds=unsubmitted_seeds)
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
        "unsubmittedSeeds": unsubmitted_seeds,
        "stopReason": stop_reason,
    }


def _new_batch(store: ProjectStore, project: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    batch = store.append_job(project, kind="cover-batch" if parameters.get("taskType") == "cover" else "candidate-batch", parameters=parameters)
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
    client_factory: Callable[..., AceStepClient] | None = None,
    engine: str = DEFAULT_ENGINE,
    quality: dict[str, Any] | None = None,
    quality_backend: Any | None = None,
) -> dict[str, Any]:
    store = ProjectStore(project_root)
    seed_list = [validate_seed(seed) for seed in seeds]
    if not math.isfinite(lm_temperature) or not 0.0 < lm_temperature <= 2.0:
        raise ValueError("lm temperature must be in (0, 2]")
    if not seed_list:
        raise ValueError("at least one seed is required")
    if len(set(seed_list)) != len(seed_list):
        raise ValueError("seeds must be unique within a batch")
    if engine not in {ENGINE, LEGACY_ENGINE}:
        raise ValueError("unknown music engine")
    if engine == LEGACY_ENGINE and model == MODEL:
        model = LEGACY_DIT_MODEL
    if engine == LEGACY_ENGINE and base_url == BASE_URL:
        base_url = LEGACY_BASE_URL
    client = (client_factory or (Music3Client if engine == ENGINE else AceStepClient))(base_url)
    source_rules = None
    with store.generation_lock():
        with store.transaction() as project:
            recover_jobs(store, project)
            changes: dict[str, Any] = {}
            if source_candidate_id:
                source = store.find_by_id(project, "candidates", "candidateId", source_candidate_id)
                original = store.find_by_id(project, "requests", "requestId", source["requestId"])["parameters"]
                source_rules = original.get("productionRules")
                source_metas = source_rules.get("inputMetas", {}) if source_rules else {}
                changes.update(style_prompt=source_rules["baseStylePrompt"] if source_rules else original.get("sourceStylePrompt", original["prompt"]),
                               lyrics=original.get("songPlan", {}).get("lyricsOriginal", original.get("sourceLyricsOriginal", original["lyrics"])), duration_seconds=original["audio_duration"],
                               bpm=source_metas.get("bpm", original.get("bpm")) or 0,
                               key_scale=source_metas.get("keyScale", original.get("key_scale")) or "",
                               time_signature=source_metas.get("timeSignature", original.get("time_signature")) or "",
                               production_rules=source_rules["selection"] if source_rules else {"version": 1, "presetId": None, "ruleIds": []})
            for field, value in (("style_prompt", style_prompt), ("lyrics", lyrics), ("bpm", bpm)):
                if value is not None:
                    if field == "style_prompt" and source_rules:
                        value = editable_style(value, original)
                    changes[field] = value
            _revise_inputs(project, **changes, reason="feedback" if feedback else "new-batch", production_rules_snapshot=source_rules)
            payloads = [
                _frozen_generation_payload(project, seed=seed, model=model, lm_model=lm_model, lm_temperature=lm_temperature,
                                           production_rules_snapshot=source_rules, engine=engine)
                for seed in seed_list
            ]
            batch = _new_batch(store, project, {
                "seeds": seed_list, "model": model, "lmModel": lm_model if engine == LEGACY_ENGINE else None, "engine": engine,
                "baseUrl": base_url, "explicitResume": False, "frozenPayloads": payloads,
                "sourceCandidateId": source_candidate_id,
            })
            if quality and quality.get("enabled"):
                plan = make_quality_plan(payloads, quality)
                batch["parameters"].update(qualityPolicy=deepcopy(quality), qualityPlan=plan,
                                           qualityRootJobId=batch["jobId"], frozenPayloads=[group[0] for group in plan])
            if feedback:
                record = _append_feedback(project, feedback, job_id=batch["jobId"])
                batch["parameters"]["feedbackId"] = record["feedbackId"]
        if quality and quality.get("enabled"):
            return run_quality_batch(store, batch_id=batch["jobId"], client=client, poll_seconds=poll_seconds,
                                     timeout_seconds=timeout_seconds, backend=quality_backend)
        return _run_batch(store, batch_id=batch["jobId"], payloads=payloads, reusable={},
                          client=client, poll_seconds=poll_seconds, timeout_seconds=timeout_seconds)


def cover_candidates(
    project_root: Path | str, *, candidate_id: str, seeds: Iterable[int],
    strength: float = 0.7, style_prompt: str | None = None, lyrics: str | None = None,
    base_url: str = DEFAULT_BASE_URL, model: str | None = None,
    poll_seconds: float = 1.0, timeout_seconds: float = 1800.0,
    feedback: dict[str, Any] | None = None,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
    quality: dict[str, Any] | None = None, quality_backend: Any | None = None,
    engine: str = DEFAULT_ENGINE,
) -> dict[str, Any]:
    """Make new versions conditioned on a verified source; project inputs stay intact."""

    if engine != LEGACY_ENGINE:
        raise ValueError("Music 3 does not support cover or reference audio; create a new song from the version's lyrics and style instead")
    if base_url == BASE_URL:
        base_url = LEGACY_BASE_URL
    if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("cover strength must be a finite number between 0 and 1")
    seed_list = [validate_seed(seed) for seed in seeds]
    if not seed_list or len(set(seed_list)) != len(seed_list):
        raise ValueError("cover seeds must be nonempty and unique")
    if lyrics is not None and not lyrics.strip():
        raise ValueError("lyrics must not be empty; use [Instrumental] for no vocals")
    store = ProjectStore(project_root)
    with store.generation_lock():
        project = store.load()
        parent = store.find_by_id(project, "candidates", "candidateId", candidate_id)
        if parent.get("status") != "ready":
            raise ValueError("cover source candidate must be ready")
        artifact = store.find_by_id(project, "artifacts", "artifactId", parent["artifactId"])
        valid, reason = store.verify_artifact(artifact)
        if not valid:
            raise ValueError(reason)
        duration = inspect_wav(store.resolve_artifact(artifact))["durationSeconds"]
        original = store.find_by_id(project, "requests", "requestId", parent["requestId"])["parameters"]
        source_rules = original.get("productionRules")
        base = source_rules["baseStylePrompt"] if source_rules else original["prompt"]
        if style_prompt is not None:
            base = editable_style(style_prompt.strip(), original)
        source_inputs = {"stylePrompt": base,
                         "lyricsNormalized": (lyrics if lyrics is not None else original.get("songPlan", {}).get("lyricsOriginal", original["lyrics"])),
                         "targetDurationSeconds": duration, "structure": original.get("project_structure")}
        for field, api_field in _META_FIELDS:
            source_inputs[field] = original.get(api_field)
        reference = {"candidateId": candidate_id, "artifactId": artifact["artifactId"],
                     "sha256": artifact["sha256"], "bytes": artifact["bytes"], "durationSeconds": duration}
        payloads = []
        for seed in seed_list:
            frozen = _frozen_generation_payload(
                {"inputs": source_inputs}, seed=seed, model=model or original.get("model", LEGACY_DIT_MODEL),
                lm_model=original.get("lm_model_path", DEFAULT_LM_MODEL), task_type="cover",
                parent_artifact_sha256=artifact["sha256"], production_rules_snapshot=source_rules, engine=LEGACY_ENGINE,
            )
            if not frozen["prompt"].strip():
                raise ValueError("cover style must not be empty without production guidance")
            frozen.update(audio_cover_strength=float(strength), coverSource=deepcopy(reference))
            payloads.append(frozen)
        # Source-conditioned edits get one check per request, without random retries.
        policy = {**deepcopy(quality), "maxAttempts": 1} if quality and quality.get("enabled") else None
        with store.transaction() as project:
            recover_jobs(store, project)
            parameters = {"taskType": "cover", "seeds": seed_list, "model": payloads[0]["model"],
                          "baseUrl": base_url, "explicitResume": False, "frozenPayloads": payloads,
                          "sourceCandidateId": candidate_id, "coverSource": reference}
            batch = _new_batch(store, project, parameters)
            if policy:
                plan = make_quality_plan(payloads, policy)
                batch["parameters"].update(qualityPolicy=policy, qualityPlan=plan,
                                          qualityRootJobId=batch["jobId"], frozenPayloads=[group[0] for group in plan])
            if feedback:
                record = _append_feedback(project, feedback, job_id=batch["jobId"])
                batch["parameters"]["feedbackId"] = record["feedbackId"]
        client = client_factory(base_url)
        if policy:
            return run_quality_batch(store, batch_id=batch["jobId"], client=client, poll_seconds=poll_seconds,
                                     timeout_seconds=timeout_seconds, backend=quality_backend)
        return _run_batch(store, batch_id=batch["jobId"], payloads=payloads, reusable={}, client=client,
                          poll_seconds=poll_seconds, timeout_seconds=timeout_seconds)


def resume_latest_batch(
    project_root: Path | str,
    *,
    job_id: str | None = None,
    base_url: str | None = None,
    poll_seconds: float = 1.0,
    timeout_seconds: float = 1800.0,
    client_factory: Callable[..., AceStepClient] | None = None,
    engine: str = DEFAULT_ENGINE,
    quality_backend: Any | None = None,
) -> dict[str, Any]:
    store = ProjectStore(project_root)
    with store.generation_lock():
        project = store.load()
        previous = resume_source(project, job_id)
        payloads = frozen_batch_payloads(project, previous)
        frozen_engine = ENGINE if payloads[0].get("engine") == ENGINE else LEGACY_ENGINE
        if any((ENGINE if item.get("engine") == ENGINE else LEGACY_ENGINE) != frozen_engine for item in payloads):
            raise ValueError("cannot resume a batch containing different music engines")
        if engine != frozen_engine:
            raise ValueError("this frozen batch belongs to the legacy ACE engine; use engine='ace-step' explicitly or start a new Music 3 generation"
                             if frozen_engine == LEGACY_ENGINE else "this frozen batch belongs to Music 3 and must resume on Music 3")
        reusable = reusable_candidates(store, project, previous, payloads)
        parameters = deepcopy(previous["parameters"])
        if parameters.get("qualityPolicy", {}).get("enabled"):
            validate_quality_plan(parameters)
        parameters.update(explicitResume=True, resumeOfJobId=previous["jobId"], frozenPayloads=payloads)
        if base_url is not None:
            parameters["baseUrl"] = base_url
        client = (client_factory or (Music3Client if frozen_engine == ENGINE else AceStepClient))(parameters["baseUrl"])
        with store.transaction() as project:
            recover_jobs(store, project)
            batch = _new_batch(store, project, parameters)
        if parameters.get("qualityPolicy", {}).get("enabled"):
            return run_quality_batch(store, batch_id=batch["jobId"], client=client, poll_seconds=poll_seconds,
                                     timeout_seconds=timeout_seconds, backend=quality_backend)
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
    quality: dict[str, Any] | None = None,
    quality_backend: Any | None = None,
    engine: str = DEFAULT_ENGINE,
) -> dict[str, Any]:
    if engine != LEGACY_ENGINE:
        raise ValueError("Music 3 does not support repaint; create a new song from the version's lyrics and style instead")
    if base_url == BASE_URL:
        base_url = LEGACY_BASE_URL
    seed = validate_seed(seed)
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
        source_rules = original.get("productionRules")
        if source_rules and style_prompt is not None:
            style_prompt = editable_style(style_prompt, original)
        source_inputs = {
            "stylePrompt": source_rules["baseStylePrompt"] if source_rules else original["prompt"],
            "lyricsNormalized": original.get("songPlan", {}).get("lyricsOriginal", original["lyrics"]),
            "targetDurationSeconds": duration, "structure": original.get("project_structure"),
        }
        for field, api_field in _META_FIELDS:
            source_inputs[field] = source_rules.get("inputMetas", {}).get(field) if source_rules else original.get(api_field)
        if lyrics is not None:
            source_inputs["lyricsNormalized"] = lyrics.replace("\r\n", "\n").replace("\r", "\n")
        edit_range = {"startSeconds": float(start_seconds), "endSeconds": float(end_seconds)}
        frozen = _frozen_generation_payload(
            {"inputs": source_inputs}, seed=seed,
            model=model or original.get("model", LEGACY_DIT_MODEL),
            lm_model=lm_model or original.get("lm_model_path", DEFAULT_LM_MODEL),
            task_type="repaint", parent_artifact_sha256=parent_artifact["sha256"],
            edit_range=edit_range, instruction=instruction,
            style_prompt=style_prompt.strip() if style_prompt is not None else None,
            repaint_strength=REPAINT_STRENGTHS[strength] if strength else None,
            production_rules_snapshot=source_rules, engine=LEGACY_ENGINE,
        )
        adapter_info = client.health()
        _require_loaded_models(adapter_info, frozen)
        with store.transaction() as project:
            recover_jobs(store, project)
            request = _append_request(project, frozen, adapter_info=adapter_info)
            job = store.append_job(project, kind="repaint-candidate", parameters={
                "requestId": request["requestId"], "parentCandidateId": parent_id, "strength": strength,
                "qualityPolicy": deepcopy(quality),
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
            quality_context={"complete": False, "preferred": False, "attempt": 1, "maxAttempts": 1,
                             "originalSeed": seed, "groupId": job["jobId"], "status": "unknown",
                             "summary": "자동 검사를 진행하고 있어요."} if quality and quality.get("enabled") else None,
        )
        if quality and quality.get("enabled"):
            candidate_id = finish_single_edit(store, candidate_id, job_id=job["jobId"], payload=frozen,
                                               policy=quality, backend=quality_backend)
            return {"jobId": job["jobId"], "candidateId": candidate_id, "recommendedCandidateId": candidate_id}
        return {"jobId": job["jobId"], "candidateId": candidate_id}


def revise_inputs(project_root: Path | str, *, engine: str = DEFAULT_ENGINE, **changes: Any) -> dict[str, Any] | None:
    """Edit title/style/lyrics/duration/metas for future requests; history is kept."""

    if engine not in {ENGINE, LEGACY_ENGINE}:
        raise ValueError("unknown music engine")
    duration = changes.get("duration_seconds")
    if engine == ENGINE and duration is not None and (not math.isfinite(duration) or not 10 <= duration <= MAX_DURATION_SECONDS):
        raise ValueError("Music 3 duration must be between 10 and 300 seconds")
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


def _copy_atomic(
    source: Path, destination: Path, *, on_staged: Callable[[Path], None] | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    retained = False
    try:
        if on_staged is not None:
            # Persist ownership before either copying or publishing. Keep this inode
            # alive until the manifest commits so recovery can safely undo publication.
            on_staged(temporary)
            retained = True
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
        if not retained:
            temporary.unlink(missing_ok=True)


def export_selected(
    project_root: Path | str, *, output: Path | str | None = None, candidate_id: str | None = None,
) -> dict[str, Any]:
    store = ProjectStore(project_root)
    with store.export_lock():
        job_id = None
        try:
            with store.transaction() as project:
                selected_id = candidate_id or project.get("selectedCandidateId")
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
                # Own the lease before recovering old exports, including an output
                # filename left by a process killed after its atomic publication.
                recover_export_jobs(store, project)
                external = None
                if output is not None:
                    requested = Path(output).expanduser().absolute()
                    external = requested.resolve()
                    if external.is_relative_to(store.root):
                        raise ValueError("export output must be outside the project; omit --output for an internal export")
                    if requested.exists() or requested.is_symlink():
                        raise FileExistsError("export output already exists; choose a new filename")
                job = store.append_job(
                    project, kind="export",
                    parameters={"candidateId": selected_id, "requestedOutput": str(output or "")},
                )
                job_id = job["jobId"]
                job["ownedOutputs"] = []
                store.transition_job(job, "running", stage="copying", progress=0.1)
            internal = store.root / "exports" / f"{job_id}-{selected_id}.wav"

            def staged(destination: Path) -> Callable[[Path], None]:
                def record(temporary: Path) -> None:
                    identity = temporary.stat()
                    with store.transaction() as project:
                        current = store.find_by_id(project, "jobs", "jobId", job_id)
                        current["ownedOutputs"].append({
                            "path": str(destination), "temporaryPath": str(temporary),
                            "device": identity.st_dev, "inode": identity.st_ino,
                            "expectedBytes": source_artifact["bytes"],
                            "expectedSha256": source_artifact["sha256"],
                        })
                return record

            store.relative_path(internal)
            _copy_atomic(source, internal, on_staged=staged(internal))
            if sha256_file(internal) != source_artifact["sha256"]:
                raise RuntimeError("export hash differs from the selected candidate")
            artifact, findings = artifact_and_findings(
                path=internal,
                project_relative_path=store.relative_path(internal),
                artifact_kind="export-wav",
                created_by_job_id=job_id,
                requested_duration_seconds=source_artifact["audio"]["durationSeconds"],
            )
            if external is not None:
                _copy_atomic(internal, external, on_staged=staged(external))
                if sha256_file(external) != artifact["sha256"]:
                    raise RuntimeError("external export hash verification failed")
                artifact["externalPath"] = str(external)
            with store.transaction() as project:
                job = store.find_by_id(project, "jobs", "jobId", job_id)
                project["artifacts"].append(artifact)
                project["findings"].extend(findings)
                job["resultRefs"] = [artifact["artifactId"]]
                project["revisions"].append({
                    "revisionId": new_id("revision"), "kind": "export",
                    "previousRevisionId": (
                        project["revisions"][-1]["revisionId"] if project["revisions"] else None
                    ),
                    "before": {},
                    "after": {"candidateId": selected_id, "exportArtifactId": artifact["artifactId"]},
                    "createdAt": utc_now(),
                })
                store.transition_job(job, "succeeded", stage="verified", progress=1.0)
            # Published audio is now a historical artifact; only staging links may go.
            cleanup_export_outputs(job, include_published=False)
            return {
                "artifactId": artifact["artifactId"],
                "path": str(internal),
                "externalPath": artifact.get("externalPath"),
                "sha256": artifact["sha256"],
            }
        except (Exception, KeyboardInterrupt) as error:
            if job_id is not None:
                try:
                    with store.transaction() as project:
                        job = store.find_by_id(project, "jobs", "jobId", job_id)
                        # A save can fail either before or after the atomic replace.
                        # The canonical status, not the mutated local dict, decides.
                        if job["status"] in ACTIVE_STATUSES:
                            job["outputCleanupErrors"] = cleanup_export_outputs(job)
                            cancelled = isinstance(error, KeyboardInterrupt)
                            job["cancelRequested"] = cancelled
                            state = "cancelled" if cancelled else "failed"
                            store.transition_job(job, state, stage=state,
                                                 error=f"{type(error).__name__}: {error}")
                        elif job["status"] == "succeeded":
                            cleanup_export_outputs(job, include_published=False)
                except (Exception, KeyboardInterrupt) as recording_error:
                    error.add_note(f"Could not record export failure: {recording_error}")
            raise
