"""Bounded music generation, evidence-based checks and immutable output finishing."""

from __future__ import annotations

import re
import math
import secrets
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .audio_quality import AudioFinishPolicy, analyze_audio_quality, finish_audio
from .execution import execute_candidate
from .jobs import ACTIVE_STATUSES, batch_lineage, cleanup_export_outputs, validate_seed
from .lyric_quality import assess_lyric_suitability, evaluate_lyric_transcript
from .qc import artifact_and_findings
from .quality_backend import AudioOnlyBackend, LocalQualityBackend
from .storage import ProjectStore, fingerprint, new_id, utc_now
from .song_planning import prepare_song_plan
from .lyrics import is_instrumental_lyrics
from .music_structure import VERSION as RHYTHM_VERSION, analyze_music_structure
from .rhythm_diagnostics import SUPPORTED_VERSIONS as DIAGNOSTICS_VERSIONS, VERSION as DIAGNOSTICS_VERSION
from .rhythm_inspection import attach_diagnostics

QUALITY_VERSION = "music-quality-v1"


def quality_policy(mode: str = "auto", *, max_attempts: int = 4) -> dict[str, Any] | None:
    if mode == "off":
        return None
    if mode not in {"auto", "audio"}:
        raise ValueError("quality mode must be auto, audio or off")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 4:
        raise ValueError("quality attempts must be between 1 and 4, including the first generation")
    return {"enabled": True, "version": QUALITY_VERSION, "maxAttempts": max_attempts,
            "lyrics": mode == "auto", "finishAudio": True, "autoSetup": True,
            "rhythmVersion": RHYTHM_VERSION, "diagnosticsVersion": DIAGNOSTICS_VERSION}


def prepare_payload(payload: dict[str, Any]) -> dict[str, Any]:
    prepared = deepcopy(payload)
    original = payload.get("songPlan", {}).get("lyricsOriginal", payload.get("sourceLyricsOriginal", payload["lyrics"]))
    if payload.get("task_type") == "cover":
        prepared["qualityPreparation"] = {"version": QUALITY_VERSION, "lyricsOriginal": original,
            "changes": [], "requestedDurationSeconds": payload["audio_duration"],
            "effectiveDurationSeconds": payload["audio_duration"],
            "preflight": assess_lyric_suitability(original, payload["audio_duration"], bpm=payload.get("bpm"))}
        return prepared
    # Song planning has already preserved the lexical content while changing line breaks/tags.
    model_lyrics = payload["lyrics"]
    normalized = unicodedata.normalize("NFC", model_lyrics.replace("\r\n", "\n").replace("\r", "\n"))
    changes = []
    lines = []
    for line in normalized.splitlines():
        # Break only unusually long prose at existing spaces/punctuation, preserving
        # every sung token and its order. Short lyric lines and rap phrasing stay intact.
        if "[" not in line and len(line) > 100:
            pieces = re.split(r"(?<=[,，.!?。！？])\s+|\s+(?=\S)", line)
            current = ""
            for piece in pieces:
                if current and len(current) + len(piece) > 64:
                    lines.append(current)
                    current = piece
                else:
                    current += (" " if current else "") + piece
            lines.append(current)
            changes.append("long_line_wrapped_at_word_boundaries")
        else:
            lines.append(line)
    normalized = "\n".join(lines)
    if normalized != model_lyrics and not changes:
        changes.append("unicode_and_newlines_normalized")
    instrumental = is_instrumental_lyrics(normalized)
    # Loop/cut/rap intent belongs to the person's own style. Music 3's rendered
    # caption also carries preset prose (e.g. "chord loop") that is not that intent.
    caption = payload.get("sourceStylePrompt", payload["prompt"]) if payload.get("engine") == "minimax-music3" else payload["prompt"]
    loop_or_cut = bool(re.search(r"\b(loop|loopable|seamless|hard cut|abrupt ending)\b|반복용|루프|갑자기.*끝", caption, re.I))
    if not instrumental and not loop_or_cut and len(normalized) <= 4084 and not re.search(r"\[(outro|ending|end)\b", normalized, re.I):
        normalized = normalized.rstrip() + "\n\n[Outro]"
        changes.append("outro_structure_tag_added")
    # No invented BPM/key, no aggressive EQ/mix instruction, and no silent lyric rewrite.
    prepared["lyrics"] = normalized
    preflight = assess_lyric_suitability(original, payload["audio_duration"], bpm=payload.get("bpm"))
    fast_delivery = bool(re.search(r"\b(rap|double[- ]time|fast delivery|speedcore)\b|빠른.*랩|속사포", caption, re.I))
    if preflight["status"] == "too_dense" and not loop_or_cut and not fast_delivery:
        # This is a conservative timing estimate, not a universal singing limit.
        # Preserve explicit rap/loop/cut intent. The original target
        # remains in project inputs; every effective duration is frozen in the request.
        minimum = math.ceil(preflight["estimatedSungSyllables"] / (6 * 0.75) / 5) * 5
        effective = min(300.0 if payload.get("engine") == "minimax-music3" else 600.0, max(float(payload["audio_duration"]), float(minimum)))
        if effective > payload["audio_duration"]:
            prepared["audio_duration"] = effective
            changes.append("duration_extended_for_dense_lyrics")
            if payload.get("songPlan"):
                controls = payload["songPlan"]["options"]
                song_plan = prepare_song_plan(original, duration_seconds=effective,
                    bpm=payload.get("bpm"), time_signature=payload.get("time_signature"),
                    preset_id=controls["presetId"], instrumental=controls["instrumental"],
                    development=controls["development"], breathing=controls["breathing"])
                prepared["songPlan"] = song_plan
                prepared["productionRules"]["songPlan"] = deepcopy(song_plan)
                prepared["lyrics"] = song_plan["lyricsPrepared"]
                if "outro_structure_tag_added" in changes:
                    prepared["lyrics"] += "\n\n[Outro]"
                changes.append("song_plan_updated_for_effective_duration")
    prepared["qualityPreparation"] = {"version": QUALITY_VERSION, "lyricsOriginal": original,
                                       "changes": list(dict.fromkeys(changes)),
                                       "requestedDurationSeconds": payload["audio_duration"],
                                       "effectiveDurationSeconds": prepared["audio_duration"], "preflight": preflight}
    if prepared.get("engine") == "minimax-music3":
        from .music3 import translate_payload
        prepared = translate_payload(prepared)
    return prepared


def make_quality_plan(payloads: list[dict[str, Any]], policy: dict[str, Any]) -> list[list[dict[str, Any]]]:
    if (policy.get("version") != QUALITY_VERSION or isinstance(policy.get("maxAttempts"), bool)
            or not isinstance(policy.get("maxAttempts"), int) or not 1 <= policy["maxAttempts"] <= 4
            or policy.get("rhythmVersion") not in (None, RHYTHM_VERSION)):
        raise ValueError("unsupported automatic quality policy")
    if policy.get("diagnosticsVersion") not in (None, *DIAGNOSTICS_VERSIONS):
        raise ValueError("unsupported rhythm diagnosis policy")
    used = {payload["seed"] for payload in payloads}
    plan = []
    for original in payloads:
        first = prepare_payload(original)
        attempts = [first]
        for _ in range(1, policy["maxAttempts"]):
            while True:
                seed = secrets.randbelow(2**32)
                if seed not in used:
                    used.add(seed)
                    break
            attempts.append({**deepcopy(first), "seed": seed})
        plan.append(attempts)
    return plan


def validate_quality_plan(parameters: dict[str, Any]) -> None:
    policy, plan, seeds = parameters.get("qualityPolicy"), parameters.get("qualityPlan"), parameters.get("seeds")
    if (not isinstance(policy, dict) or policy.get("version") != QUALITY_VERSION
            or policy.get("rhythmVersion") not in (None, RHYTHM_VERSION)
            or policy.get("diagnosticsVersion") not in (None, *DIAGNOSTICS_VERSIONS)
            or isinstance(policy.get("maxAttempts"), bool) or not isinstance(policy.get("maxAttempts"), int)
            or not 1 <= policy["maxAttempts"] <= 4 or not isinstance(plan, list)
            or not isinstance(seeds, list) or not plan or len(plan) != len(seeds)):
        raise ValueError("invalid automatic quality snapshot")
    all_seeds = []
    for seed, group in zip(seeds, plan):
        validate_seed(seed)
        if not isinstance(group, list) or len(group) != policy["maxAttempts"]:
            raise ValueError("automatic quality attempts exceed the saved budget")
        if group[0].get("seed") != seed:
            raise ValueError("automatic quality snapshot changed the original seed")
        for payload in group:
            validate_seed(payload.get("seed"))
            all_seeds.append(payload["seed"])
            if not isinstance(payload.get("lyrics"), str) or not isinstance(payload.get("prompt"), str):
                raise ValueError("automatic quality snapshot lacks frozen lyrics or style")
            if fingerprint({key: value for key, value in payload.items() if key != "seed"}) != fingerprint(
                {key: value for key, value in group[0].items() if key != "seed"}
            ):
                raise ValueError("automatic quality retries must keep the original frozen inputs")
    if parameters.get("frozenPayloads") != [group[0] for group in plan]:
        raise ValueError("automatic quality plan does not match the batch snapshot")
    if len(set(all_seeds)) != len(all_seeds):
        raise ValueError("automatic quality attempt seeds must be unique")


def _candidate(store: ProjectStore, candidate_id: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
    project = store.load()
    candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
    valid, reason = store.verify_artifact(artifact)
    if not valid:
        raise ValueError(reason)
    return candidate, artifact, store.resolve_artifact(artifact)


def _quality_rank(report: dict[str, Any]) -> tuple[float, ...]:
    audio = report.get("audio", {})
    lyrics = report.get("lyrics", {})
    known = lyrics.get("status") in {"pass", "warning"}
    return (float(not audio.get("retryEligible", False)),
            float(lyrics.get("status") in {"pass", "not_applicable"}),
            float(not report.get("retryReasons")),
            float(lyrics.get("orderedCoverage") or 0) if known else 0,
            float(audio.get("technicalScore", 0)))


def _measure_rhythm(path: Path, payload: dict[str, Any], artifact_sha256: str) -> dict[str, Any]:
    """An uncertain music estimate must not discard a usable generation."""
    try:
        report = analyze_music_structure(path, requested_bpm=payload.get("bpm"),
            time_signature=payload.get("time_signature"),
            style_prompt=payload.get("sourceStylePrompt", payload.get("prompt", "")))
    except Exception as error:
        report = {"version": RHYTHM_VERSION, "status": "unknown",
                  "requestedBpm": payload.get("bpm"), "estimatedBpm": None,
                  "confidence": 0.0, "findings": [],
                  "error": f"{type(error).__name__}: {error}"}
    return {**report, "measuredArtifactSha256": artifact_sha256}


def _assessment_status(audio: dict[str, Any], lyrics: dict[str, Any], rhythm: dict[str, Any] | None,
                       reasons: list[str]) -> str:
    if (reasons or audio.get("automaticStatus") == "needs_review"
            or lyrics.get("status") == "warning" or (rhythm or {}).get("status") == "needs_review"):
        return "attention"
    if lyrics.get("status") == "unknown" or (rhythm or {}).get("status") == "unknown":
        return "unknown"
    return "passed"


def _assessment_summary(status: str, *, instrumental: bool) -> str:
    if status == "attention":
        return "자동 검사에서 확인할 부분이 있어요. 직접 들어 비교해 주세요."
    if status == "unknown":
        return "음원은 보존했지만 자동 검사 일부를 확정하지 못했어요."
    return "연주곡 음원 검사를 마쳤어요. 직접 들어 확인해 주세요." if instrumental else "가사 일치와 음원 검사를 마쳤어요. 직접 들어 확인해 주세요."


def assess_candidate(store: ProjectStore, candidate_id: str, *, policy: dict[str, Any], backend: Any,
                     batch_id: str, context: dict[str, Any]) -> dict[str, Any]:
    candidate, artifact, path = _candidate(store, candidate_id)
    if candidate.get("quality", {}).get("complete"):
        return candidate["quality"]
    with store.transaction() as project:
        check = store.append_job(project, kind="quality-check", parameters={"candidateId": candidate_id}, parent_job_id=batch_id)
        store.transition_job(check, "running", stage="quality_audio")

    def stage(name: str) -> None:
        with store.transaction() as project:
            current = store.find_by_id(project, "jobs", "jobId", check["jobId"])
            current["stage"] = name
            batch = store.find_by_id(project, "jobs", "jobId", batch_id)
            batch["stage"] = name

    try:
        audio = analyze_audio_quality(path, requested_duration_seconds=context["payload"]["audio_duration"])
        rhythm = None
        if policy.get("rhythmVersion") == RHYTHM_VERSION:
            stage("quality_rhythm")
            rhythm = _measure_rhythm(path, context["payload"], artifact["sha256"])
        original_lyrics = context["payload"].get("qualityPreparation", {}).get("lyricsOriginal", context["payload"]["lyrics"])
        preflight = assess_lyric_suitability(original_lyrics, context["payload"]["audio_duration"], bpm=context["payload"].get("bpm"))
        if preflight["instrumental"]:
            observation = {"available": False, "reliable": False, "text": "", "segments": [],
                           "loudness": backend.measure_loudness(path) if hasattr(backend, "measure_loudness") else {"status": "unknown"}}
        else:
            stage("quality_lyrics")
            observation = backend.analyze(path, progress=lambda message: stage("quality_lyrics: " + message))
        if rhythm is not None and policy.get("diagnosticsVersion") in DIAGNOSTICS_VERSIONS:
            stage("quality_rhythm")
            rhythm = attach_diagnostics(rhythm, path, duration_seconds=artifact["audio"]["durationSeconds"],
                source_sha256=artifact["sha256"], separation=observation.get("separation"),
                style_prompt=context["payload"].get("sourceStylePrompt", context["payload"].get("prompt", "")),
                diagnostics_version=policy["diagnosticsVersion"])
        lyrics = evaluate_lyric_transcript(original_lyrics, observation, duration_seconds=artifact["audio"]["durationSeconds"])
        reasons = [finding["check"] for finding in audio["findings"] if finding.get("retryEligible")]
        reasons.extend(lyrics.get("retryReasons", []))
        status = _assessment_status(audio, lyrics, rhythm, reasons)
        summary = _assessment_summary(status, instrumental=preflight["instrumental"])
        report = {**context["metadata"], "version": QUALITY_VERSION, "complete": True,
                  "status": status, "summary": summary, "sourceArtifactSha256": artifact["sha256"],
                  "preferred": False, "audio": audio, "lyrics": lyrics, "preflight": preflight,
                  "preparation": context["payload"].get("qualityPreparation"),
                  "observation": observation, "processing": None, "retryReasons": list(dict.fromkeys(reasons)),
                  "score": audio["technicalScore"], "scope": "technical_integrity_and_transcript_match"}
        if rhythm is not None:
            report["rhythm"] = rhythm
            report["scope"] = "technical_integrity_transcript_match_and_rhythm_observations"
        with store.transaction() as project:
            current = store.find_by_id(project, "candidates", "candidateId", candidate_id)
            current["quality"] = report
            for finding in [*audio["findings"], *(rhythm or {}).get("findings", [])]:
                record = {**finding, "severity": "failure" if finding["severity"] == "error" else finding["severity"],
                          "findingId": new_id("finding"), "artifactId": artifact["artifactId"], "createdAt": utc_now()}
                project["findings"].append(record)
                current["findingIds"].append(record["findingId"])
            check = store.find_by_id(project, "jobs", "jobId", check["jobId"])
            store.transition_job(check, "succeeded", stage="quality_checked", progress=1)
            check["resultRefs"] = [candidate_id]
        return report
    except (Exception, KeyboardInterrupt) as error:
        if not isinstance(error, KeyboardInterrupt):
            saved = store.load()
            persisted = store.find_by_id(saved, "jobs", "jobId", check["jobId"])
            existing = store.find_by_id(saved, "candidates", "candidateId", candidate_id)
            if persisted["status"] == "succeeded" and existing.get("quality", {}).get("complete"):
                _candidate(store, candidate_id)
                return existing["quality"]
        with store.transaction() as project:
            current = store.find_by_id(project, "jobs", "jobId", check["jobId"])
            if current["status"] in ACTIVE_STATUSES:
                state = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
                store.transition_job(current, state, stage=state, error=f"{type(error).__name__}: {error}")
        raise


def finalize_candidate(store: ProjectStore, candidate_id: str, *, batch_id: str) -> str:
    candidate, source_artifact, source = _candidate(store, candidate_id)
    project = store.load()
    # A completed finishing job can be reused only within an explicitly resumed batch.
    lineage = batch_lineage(project, store.find_by_id(project, "jobs", "jobId", batch_id))
    jobs = {job["jobId"]: job for job in project["jobs"]}
    for existing in reversed(project["candidates"]):
        if (existing.get("quality", {}).get("processing") or {}).get("sourceCandidateId") != candidate_id:
            continue
        artifact = store.find_by_id(project, "artifacts", "artifactId", existing["artifactId"])
        job = jobs.get(artifact.get("createdByJobId"))
        if job and job.get("parentJobId") in lineage and job["status"] == "succeeded" and store.verify_artifact(artifact)[0]:
            return existing["candidateId"]
    original = store.find_by_id(project, "requests", "requestId", candidate["requestId"])
    with store.transaction() as project:
        job = store.append_job(project, kind="audio-finish", parameters={"sourceCandidateId": candidate_id}, parent_job_id=batch_id)
        store.transition_job(job, "running", stage="finalizing")
        batch = store.find_by_id(project, "jobs", "jobId", batch_id)
        batch["stage"] = "finalizing"
    destination = store.root / "artifacts" / "finished" / f"{job['jobId']}.wav"
    relative = store.relative_path(destination)
    staged_record: dict[str, Any] | None = None
    try:
        def staged(record: dict[str, Any]) -> None:
            nonlocal staged_record
            staged_record = record
            with store.transaction() as project:
                current = store.find_by_id(project, "jobs", "jobId", job["jobId"])
                current["ownedOutputs"] = [record]

        loudness = candidate["quality"].get("observation", {}).get("loudness", {})
        true_peak = loudness.get("truePeakDbtp")
        ceiling = 0.98
        if (loudness.get("status") in {"measured", "inconclusive"} and not isinstance(true_peak, bool)
                and isinstance(true_peak, (int, float)) and math.isfinite(true_peak) and -1 < true_peak <= 12):
            # Linear attenuation only: use measured inter-sample headroom, never
            # compress or boost music to chase a fixed loudness target.
            sample_peak = candidate["quality"]["audio"]["metrics"]["peak"]
            ceiling = min(ceiling, max(0.001, sample_peak * 10 ** ((-1 - true_peak) / 20)))
        processing = finish_audio(source, destination, policy=AudioFinishPolicy(peak_ceiling=ceiling), on_staged=staged)
        artifact, findings = artifact_and_findings(path=destination, project_relative_path=relative,
                                                 artifact_kind="finished-audio", created_by_job_id=job["jobId"],
                                                 requested_duration_seconds=source_artifact["audio"]["durationSeconds"])
        artifact.update(sourceArtifactId=source_artifact["artifactId"], sourceArtifactSha256=source_artifact["sha256"], processing=processing["processing"])
        report = deepcopy(candidate["quality"])
        if report.get("rhythm") is not None:
            # Gain/DC/edge finishing changes frame levels and spectrum. Measure the
            # published file and attach its own hash instead of copying raw values.
            report["rhythm"] = _measure_rhythm(destination, original["parameters"], artifact["sha256"])
            if candidate["quality"]["rhythm"].get("diagnostics") is not None:
                report["rhythm"] = attach_diagnostics(report["rhythm"], destination,
                    duration_seconds=artifact["audio"]["durationSeconds"], source_sha256=artifact["sha256"],
                    separation=candidate["quality"].get("observation", {}).get("separation"),
                    backing_source_sha256=source_artifact["sha256"],
                    style_prompt=original["parameters"].get("sourceStylePrompt", original["parameters"].get("prompt", "")),
                    diagnostics_version=candidate["quality"]["rhythm"]["diagnostics"]["version"])
            # Keep the original integrity/transcript evidence (attenuation cannot
            # repair clipping), but don't retain stale rhythm status after reanalysis.
            report["status"] = _assessment_status(report["audio"], report["lyrics"], report["rhythm"], report["retryReasons"])
            report["summary"] = _assessment_summary(report["status"], instrumental=report["lyrics"].get("status") == "not_applicable")
            findings.extend({**finding, "findingId": new_id("finding"), "artifactId": artifact["artifactId"],
                             "createdAt": utc_now()} for finding in report["rhythm"].get("findings", []))
        report["processing"] = {**processing["processing"], "sourceCandidateId": candidate_id,
                                "sourceArtifactId": source_artifact["artifactId"], "existingClippingRepaired": False,
                                "outputQuality": processing["outputQuality"]}
        parameters = {**deepcopy(original["parameters"]), "task_type": "postprocess",
                      "source_artifact_sha256": source_artifact["sha256"], "audio_processing": processing["processing"]}
        request = {"requestId": new_id("request"), "fingerprint": fingerprint(parameters), "adapter": "pcm-finish",
                   "adapterVersion": QUALITY_VERSION, "adapterInfo": {"implementation": QUALITY_VERSION},
                   "parameters": parameters, "createdAt": utc_now()}
        result = {"candidateId": new_id("candidate"), "requestId": request["requestId"], "artifactId": artifact["artifactId"],
                  "findingIds": [finding["findingId"] for finding in findings], "status": "ready",
                  "parentCandidateId": candidate_id, "editRange": deepcopy(candidate.get("editRange")),
                  "contextRange": {"startSeconds": 0.0, "endSeconds": source_artifact["audio"]["durationSeconds"]},
                  "quality": report, "humanReview": {"status": "unreviewed", "rating": None, "notes": [], "updatedAt": None},
                  "createdAt": utc_now()}
        with store.transaction() as project:
            project["requests"].append(request)
            project["artifacts"].append(artifact)
            project["findings"].extend(findings)
            project["candidates"].append(result)
            project["revisions"].append({"revisionId": new_id("revision"), "kind": "audio-finish",
                "previousRevisionId": project["revisions"][-1]["revisionId"] if project["revisions"] else None,
                "before": {"candidateId": candidate_id, "artifactId": source_artifact["artifactId"]},
                "after": {"candidateId": result["candidateId"], "artifactId": artifact["artifactId"]},
                "createdAt": utc_now()})
            current = store.find_by_id(project, "jobs", "jobId", job["jobId"])
            current["resultRefs"] = [result["candidateId"], artifact["artifactId"]]
            store.transition_job(current, "succeeded", stage="finalized", progress=1)
        cleanup_export_outputs(current, include_published=False)
        return result["candidateId"]
    except (Exception, KeyboardInterrupt) as error:
        try:
            with store.transaction() as project:
                current = store.find_by_id(project, "jobs", "jobId", job["jobId"])
                if current["status"] in ACTIVE_STATUSES:
                    if not current.get("ownedOutputs") and staged_record:
                        current["ownedOutputs"] = [staged_record]
                    current["outputCleanupErrors"] = cleanup_export_outputs(current)
                    state = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
                    store.transition_job(current, state, stage=state, error=f"{type(error).__name__}: {error}")
                elif current["status"] == "succeeded":
                    cleanup_export_outputs(current, include_published=False)
                    if not isinstance(error, KeyboardInterrupt):
                        for reference in current.get("resultRefs", []):
                            result = next((item for item in project["candidates"] if item["candidateId"] == reference), None)
                            if result:
                                artifact = store.find_by_id(project, "artifacts", "artifactId", result["artifactId"])
                                if store.verify_artifact(artifact)[0]:
                                    return result["candidateId"]
        except (Exception, KeyboardInterrupt) as recording_error:
            error.add_note(f"Could not record audio finishing failure: {recording_error}")
        raise


def finish_single_edit(store: ProjectStore, candidate_id: str, *, job_id: str,
                       payload: dict[str, Any], policy: dict[str, Any], backend: Any | None = None) -> str:
    """Check a complete repaint result once; never re-submit an edit automatically."""
    backend = backend or (LocalQualityBackend(auto_setup=policy.get("autoSetup", True)) if policy.get("lyrics") else AudioOnlyBackend())
    metadata = {"attempt": 1, "maxAttempts": 1, "originalSeed": payload["seed"], "groupId": job_id,
                "preferred": False, "complete": False, "status": "unknown", "summary": "자동 검사를 진행하고 있어요."}
    try:
        assess_candidate(store, candidate_id, policy=policy, backend=backend, batch_id=job_id,
                         context={"payload": payload, "metadata": metadata})
    except Exception as error:
        with store.transaction() as project:
            candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
            candidate["quality"] = {**metadata, "version": QUALITY_VERSION, "complete": True,
                "status": "unknown", "summary": "수정 음원은 저장했지만 자동 검사를 완료하지 못했어요.",
                "audio": {}, "lyrics": {"status": "unknown"}, "processing": None, "retryReasons": [],
                "score": None, "inspectionError": f"{type(error).__name__}: {error}"}
    try:
        result = finalize_candidate(store, candidate_id, batch_id=job_id) if policy.get("finishAudio") else candidate_id
    except Exception as error:
        _candidate(store, candidate_id)
        result = candidate_id
        with store.transaction() as project:
            candidate = store.find_by_id(project, "candidates", "candidateId", result)
            candidate["quality"]["finishingError"] = f"{type(error).__name__}: {error}"
            candidate["quality"]["summary"] += " 재생본 정리를 마치지 못해 원래 수정 음원을 보존했어요."
    with store.transaction() as project:
        candidate = store.find_by_id(project, "candidates", "candidateId", result)
        candidate["quality"].update(preferred=True, attemptsUsed=1, assessedAttempts=1)
        current = store.find_by_id(project, "jobs", "jobId", job_id)
        current["resultRefs"] = [result]
        current["stage"] = "completed"
        project["recommendedCandidateId"] = result
    return result


def run_quality_batch(store: ProjectStore, *, batch_id: str, client: Any, poll_seconds: float,
                      timeout_seconds: float, backend: Any | None = None) -> dict[str, Any]:
    from .workflow import _api_payload, _append_request, _require_loaded_models
    project = store.load()
    batch = store.find_by_id(project, "jobs", "jobId", batch_id)
    policy = batch["parameters"]["qualityPolicy"]
    plan = deepcopy(batch["parameters"]["qualityPlan"])
    validate_quality_plan(batch["parameters"])
    backend = backend or (LocalQualityBackend(auto_setup=policy.get("autoSetup", True)) if policy.get("lyrics") else AudioOnlyBackend())
    lineage = batch_lineage(project, batch)
    reused = []
    stop_submissions = False

    def failure(seed: int, error: BaseException, *, stage: str, fatal: bool = False) -> None:
        with store.transaction() as project:
            current = store.find_by_id(project, "jobs", "jobId", batch_id)
            entry = {"seed": str(seed), "stage": stage, "error": f"{type(error).__name__}: {error}"}
            current.setdefault("attemptFailures", []).append(entry)
            if fatal and not any(record["seed"] == str(seed) for record in current["failures"]):
                current["failures"].append(entry)

    try:
        for slot, attempts in enumerate(plan):
            original_seed = batch["parameters"]["seeds"][slot]
            current_project = store.load()
            group_id = batch["parameters"].get("qualityRootJobId", batch_id) + f":{slot}"
            completed = None
            jobs = {job["jobId"]: job for job in current_project["jobs"]}
            for candidate in reversed(current_project["candidates"]):
                report = candidate.get("quality", {})
                if not report.get("complete") or not report.get("preferred") or report.get("groupId") != group_id:
                    continue
                artifact = store.find_by_id(current_project, "artifacts", "artifactId", candidate["artifactId"])
                job = jobs.get(artifact.get("createdByJobId"))
                attempt_number = report.get("attempt")
                if not isinstance(attempt_number, int) or not 1 <= attempt_number <= len(attempts):
                    continue
                source_id = (report.get("processing") or {}).get("sourceCandidateId", candidate["candidateId"])
                source = next((item for item in current_project["candidates"] if item["candidateId"] == source_id), None)
                if source is None:
                    continue
                source_artifact = store.find_by_id(current_project, "artifacts", "artifactId", source["artifactId"])
                source_job = jobs.get(source_artifact.get("createdByJobId"))
                source_request = store.find_by_id(current_project, "requests", "requestId", source["requestId"])
                matches_request = (source_job and source_job.get("parentJobId") in lineage
                    and source_job["kind"] == "generate-candidate" and source_job["status"] == "succeeded"
                    and source_job["parameters"].get("qualitySlot") == slot
                    and source_job["parameters"].get("qualityAttempt") == attempt_number
                    and fingerprint(source_request["parameters"]) == fingerprint(attempts[attempt_number - 1]))
                if (matches_request and job and job.get("parentJobId") in lineage and job["status"] == "succeeded"
                        and store.verify_artifact(artifact)[0]):
                    completed = candidate["candidateId"]
                    break
            if completed:
                with store.transaction() as project:
                    current = store.find_by_id(project, "jobs", "jobId", batch_id)
                    current["resultRefs"].append(completed)
                    current["reusedCandidateIds"].append(completed)
                    current["progress"] = (slot + 1) / len(plan)
                    current["stage"] = "reused quality-checked version"
                reused.append(completed)
                continue
            scored = []
            for attempt, payload in enumerate(attempts, 1):
                current_project = store.load()
                requests = {record["requestId"]: record for record in current_project["requests"]}
                jobs = {record["jobId"]: record for record in current_project["jobs"]}
                artifacts = {record["artifactId"]: record for record in current_project["artifacts"]}
                consumed = [job for job in current_project["jobs"] if job["kind"] == "generate-candidate"
                            and job.get("parentJobId") in lineage and job["parameters"].get("qualitySlot") == slot
                            and job["parameters"].get("qualityAttempt") == attempt]
                existing = None
                # Reuse only this explicit resume chain's exact, verified frozen attempt.
                for candidate in reversed(current_project["candidates"]):
                    artifact = artifacts[candidate["artifactId"]]
                    job = jobs.get(artifact.get("createdByJobId"))
                    if (job and job.get("parentJobId") in lineage and job["kind"] == "generate-candidate"
                            and job["status"] == "succeeded" and job["parameters"].get("qualitySlot") == slot
                            and job["parameters"].get("qualityAttempt") == attempt
                            and fingerprint(requests[candidate["requestId"]]["parameters"]) == fingerprint(payload)
                            and store.verify_artifact(artifact)[0]):
                        existing = candidate["candidateId"]
                        break
                metadata = {"attempt": attempt, "maxAttempts": policy["maxAttempts"], "originalSeed": original_seed,
                            "groupId": batch["parameters"].get("qualityRootJobId", batch_id) + f":{slot}",
                            "preferred": False, "complete": False, "status": "unknown", "summary": "자동 검사를 진행하고 있어요."}
                if existing:
                    candidate_id = existing
                else:
                    if consumed:
                        continue  # Interrupted/failed submission still consumes its frozen attempt.
                    if stop_submissions:
                        break
                    try:
                        health = client.health()
                        _require_loaded_models(health, payload)
                        with store.transaction() as project:
                            request = _append_request(project, payload, adapter_info=health)
                            job = store.append_job(project, kind="generate-candidate", parameters={"requestId": request["requestId"],
                                "seed": payload["seed"], "qualitySlot": slot, "qualityAttempt": attempt}, parent_job_id=batch_id)
                            store.transition_job(job, "running", stage="submitting")
                            current = store.find_by_id(project, "jobs", "jobId", batch_id)
                            current["stage"] = "quality_retry" if attempt > 1 else "submitting"
                        candidate_id = execute_candidate(store, client, request=request, job_id=job["jobId"],
                            api_payload=_api_payload(payload), poll_seconds=poll_seconds, timeout_seconds=timeout_seconds,
                            quality_context=metadata)
                    except Exception as error:
                        failure(original_seed, error, stage="generation")
                        # A timeout may leave remote inference running. Do not add more work automatically.
                        if isinstance(error, TimeoutError) or any(term in str(error) for term in
                            ("API unavailable", "API HTTP", "unhealthy ACE", "unhealthy Music 3", "Music 3 server", "not the requested")):
                            stop_submissions = True
                            break
                        continue
                try:
                    report = assess_candidate(store, candidate_id, policy=policy, backend=backend, batch_id=batch_id,
                                              context={"payload": payload, "metadata": metadata})
                except Exception as error:
                    failure(original_seed, error, stage="quality_inspection")
                    # Inspector failure cannot justify another generation. Keep the original usable.
                    report = {**metadata, "version": QUALITY_VERSION, "complete": True, "status": "unknown", "retryReasons": [],
                              "summary": "음원은 저장했지만 자동 검사를 완료하지 못했어요.",
                              "audio": {"technicalScore": 0}, "lyrics": {"status": "unknown"}, "score": None}
                    with store.transaction() as project:
                        store.find_by_id(project, "candidates", "candidateId", candidate_id)["quality"] = report
                scored.append((candidate_id, report, existing is not None))
                with store.transaction() as project:
                    current = store.find_by_id(project, "jobs", "jobId", batch_id)
                    current.setdefault("attemptCandidateIds", []).append(candidate_id)
                    current["progress"] = (slot + min(0.9, attempt / policy["maxAttempts"])) / len(plan)
                if not report["retryReasons"]:
                    break
            if not scored:
                failure(original_seed, RuntimeError("No usable audio remains within the four-attempt budget"), stage="attempt_budget", fatal=True)
                continue
            chosen, report, was_reused = max(scored, key=lambda item: _quality_rank(item[1]))
            try:
                final_id = finalize_candidate(store, chosen, batch_id=batch_id) if policy.get("finishAudio") else chosen
            except Exception as error:
                failure(original_seed, error, stage="audio_finishing")
                _candidate(store, chosen)  # Never fall back to a damaged original.
                final_id = chosen
            with store.transaction() as project:
                winner = store.find_by_id(project, "candidates", "candidateId", final_id)
                for previous in project["candidates"]:
                    if previous.get("quality", {}).get("groupId") == winner["quality"]["groupId"]:
                        previous["quality"]["preferred"] = previous["candidateId"] == final_id
                winner["quality"]["attemptsUsed"] = len({job["parameters"].get("qualityAttempt") for job in project["jobs"]
                    if job["kind"] == "generate-candidate" and job.get("parentJobId") in lineage
                    and job["parameters"].get("qualitySlot") == slot})
                winner["quality"]["assessedAttempts"] = len(scored)
                current = store.find_by_id(project, "jobs", "jobId", batch_id)
                current["resultRefs"].append(final_id)
                current["progress"] = (slot + 1) / len(plan)
                if was_reused and final_id in {candidate["candidateId"] for candidate in current_project["candidates"]}:
                    current["reusedCandidateIds"].append(final_id)
                    reused.append(final_id)
        with store.transaction() as project:
            current = store.find_by_id(project, "jobs", "jobId", batch_id)
            candidates = [store.find_by_id(project, "candidates", "candidateId", candidate_id) for candidate_id in current["resultRefs"]]
            recommendation = max(candidates, key=lambda candidate: _quality_rank(candidate["quality"]))["candidateId"] if candidates else None
            current["recommendedCandidateId"] = recommendation
            if recommendation:
                project["recommendedCandidateId"] = recommendation
            state = "partial" if candidates and current["failures"] else "succeeded" if candidates else "failed"
            store.transition_job(current, state, stage="quality_completed" if candidates else "failed", progress=1)
        return {"batchJobId": batch_id, "status": current["status"], "candidateIds": current["resultRefs"],
                "newCandidateIds": [candidate_id for candidate_id in current["resultRefs"] if candidate_id not in reused],
                "reusedCandidateIds": reused, "recommendedCandidateId": recommendation,
                "attemptCandidateIds": current.get("attemptCandidateIds", []), "failures": current["failures"]}
    except (Exception, KeyboardInterrupt) as error:
        with store.transaction() as project:
            current = store.find_by_id(project, "jobs", "jobId", batch_id)
            if current["status"] in ACTIVE_STATUSES:
                cancelled = isinstance(error, KeyboardInterrupt)
                current["cancelRequested"] = cancelled
                store.transition_job(current, "cancelled" if cancelled else "failed", stage="cancelled" if cancelled else "failed",
                                     error=f"{type(error).__name__}: {error}")
        raise
