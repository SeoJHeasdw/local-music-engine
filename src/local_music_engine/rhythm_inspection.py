"""Source-bound rhythm diagnosis and append-only inspections of saved candidates."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
import wave

from .music_structure import analyze_music_structure
from .percussion_analysis import VERSION as PERCUSSION_VERSION, analyze_percussion
from .rhythm_diagnostics import (VERSION as DIAGNOSTICS_VERSION, LEGACY_VERSION, MAX_EVENTS, SIGNAL_VERSION,
                                 SUPPORTED_VERSIONS, contextualize_tempo_observations, contextualize_timing_events,
                                 diagnose_rhythm, validate_expected_sections)
from .rhythm_context import add_rhythm_context
from .rhythm_evidence import load_cached_stem_evidence, separator_frame_inputs
from .rhythm_intent import RhythmIntent, load_rhythm_intent
from .storage import ProjectStore, atomic_write_json, new_id, sha256_file, utc_now
from .timbre_tracking import VERSION as HIT_TIMING_VERSION, analyze_timbre_timing


def _hit_timing(audio: Path, source_sha256: str, sections: list[dict[str, Any]]) -> dict[str, Any]:
    """Hits measured against the track's own repetitions; a failed measurement is unknown, never a pass."""
    try:
        timing = analyze_timbre_timing(audio)
    except Exception as error:
        return {"version": HIT_TIMING_VERSION, "events": [], "supportedRangesSeconds": [], "supportedFraction": 0.0,
            "check": {"status": "unknown", "reason": f"Hit timing was not measured: {type(error).__name__}: {error}",
                      "source": {"kind": "full_mix_pcm", "method": "hits linked to their own repetitions", "reliability": 0},
                      "observed": {}, "retryEligible": False}}
    if timing["sourceArtifactSha256"] != source_sha256:
        raise ValueError("Hit timing is not bound to the current audio")
    contextualize_timing_events(timing["check"], timing["events"], sections)
    return timing


def attach_diagnostics(rhythm: dict[str, Any], audio: Path, *, duration_seconds: float,
                       source_sha256: str, style_prompt: str = "", separation: dict[str, Any] | None = None,
                       stem_inputs: dict[str, Any] | None = None,
                       backing_source_sha256: str | None = None,
                       expected_sections: list[dict[str, Any]] | None = None,
                       diagnostics_version: str = DIAGNOSTICS_VERSION) -> dict[str, Any]:
    """HPSS evidence is independent of semantic backing/vocal measurements."""
    if diagnostics_version not in SUPPORTED_VERSIONS:
        raise ValueError("Unsupported rhythm diagnostics version")
    if expected_sections is not None:
        expected_sections = validate_expected_sections(expected_sections, duration_seconds)
    if diagnostics_version != LEGACY_VERSION:
        rhythm = contextualize_tempo_observations(rhythm, expected_sections or [])
    stem_hash = backing_source_sha256 or source_sha256
    supplied_percussion = (separation or {}).get("percussionEvidence") or (stem_inputs or {}).get("percussion")
    try:
        if isinstance(supplied_percussion, dict) and supplied_percussion.get("sourceArtifactSha256") == stem_hash:
            percussion = deepcopy(supplied_percussion)
            if stem_hash != source_sha256:
                percussion["source"]["method"] = "원본 정리 전 측정 · " + percussion["source"]["method"]
                percussion["source"]["provenanceStatus"] = "source_before_linear_audio_finish"
        else:
            percussion = analyze_percussion(audio)
    except Exception as error:
        percussion = {"version": PERCUSSION_VERSION, "status": "unknown", "pulse": {"bpm": None, "confidence": 0},
            "source": {"kind": "percussive_estimate", "method": "median HPSS unavailable", "reliability": 0},
            "onsetTimesSeconds": [], "beatCandidatesSeconds": [], "segments": [],
            "error": f"{type(error).__name__}: {error}"}
    stems = stem_inputs if stem_inputs is not None else separator_frame_inputs(separation, source_sha256=stem_hash)
    if stem_hash != source_sha256:
        # Finishing changes gain/DC/edges, without generating new musical timing.
        # Retain source observations as such; never claim re-separated playback.
        stems = deepcopy(stems)
        for key in ("accompaniment", "vocal"):
            if key in stems:
                stems[key]["source"]["method"] = "원본 정리 전 측정 · " + stems[key]["source"]["method"]
                stems[key]["source"]["provenanceStatus"] = "source_before_linear_audio_finish"
    diagnosis = diagnose_rhythm(duration_seconds=duration_seconds, percussion=percussion,
        accompaniment=stems.get("accompaniment"), vocal=stems.get("vocal"), style_prompt=style_prompt,
        expected_sections=expected_sections, diagnostics_version=diagnostics_version,
        event_limit=None if diagnostics_version in (SIGNAL_VERSION, DIAGNOSTICS_VERSION) else MAX_EVENTS)
    context_percussion = hit_timing = None
    if diagnostics_version in (SIGNAL_VERSION, DIAGNOSTICS_VERSION):
        from .signal_diagnostics import inspect_signal_dropouts
        signal = inspect_signal_dropouts(audio, expected_sections=expected_sections)
        if signal["sourceArtifactSha256"] != source_sha256:
            raise ValueError("Signal diagnosis is not bound to the current audio")
        diagnosis["signalVersion"] = signal["version"]
        diagnosis["checks"]["mixContinuity"] = signal["check"]
        combined = [*diagnosis["events"], *signal["events"]]
        scope = "observed_percussion_separated_backing_and_digital_mix_continuity"
        if diagnostics_version == DIAGNOSTICS_VERSION:
            hit_timing = _hit_timing(audio, source_sha256, expected_sections or [])
            diagnosis["hitTimingVersion"] = hit_timing["version"]
            diagnosis["checks"]["hitTiming"] = hit_timing["check"]
            combined.extend(deepcopy(hit_timing["events"]))
            scope = "observed_percussion_separated_backing_digital_mix_continuity_and_repeated_hit_timing"
        priority = lambda event: (event["severity"] not in {"warning", "error"}, event["startSeconds"], event["check"])
        retained = sorted(sorted(combined, key=priority)[:MAX_EVENTS], key=lambda event: (event["startSeconds"], event["check"]))
        diagnosis.update(events=retained, findings=retained, maximumEvents=MAX_EVENTS,
            totalEventCount=len(combined), omittedEventCount=len(combined) - len(retained),
            omittedWarningCount=sum(event["severity"] in {"warning", "error"} for event in combined)
                - sum(event["severity"] in {"warning", "error"} for event in retained),
            eventsTruncated=len(combined) > len(retained), scope=scope)
        diagnosis["status"] = ("needs_review" if any(event["severity"] in {"warning", "error"} for event in combined)
            or any(check["status"] == "needs_review" for check in diagnosis["checks"].values()) else
            "unknown" if any(check["status"] == "unknown" for check in diagnosis["checks"].values()) else "observed")
        proxy = percussion
        if any(event["category"] in {"backing_dropout", "arrangement_break"} for event in diagnosis["events"]) and not percussion.get("frameSeries"):
            # Stored isolated drums can supply timing while a separately read,
            # current HPSS proxy supplies transient energy for cross-cue checks.
            try:
                context_percussion = analyze_percussion(audio)
                context_percussion["sourceArtifactSha256"] = source_sha256
                proxy = context_percussion
            except Exception:
                proxy = None
        elif percussion.get("sourceArtifactSha256") is None:
            proxy = {**percussion, "sourceArtifactSha256": source_sha256}
        diagnosis = add_rhythm_context(diagnosis, duration_seconds=duration_seconds, source_sha256=source_sha256,
            accompaniment=stems.get("accompaniment"), vocal=stems.get("vocal"), percussion=proxy, structure=rhythm)
    diagnosis["percussionArtifactSha256"] = percussion.get("sourceArtifactSha256", source_sha256)
    diagnosis["backingEvidenceArtifactSha256"] = stem_hash if stems.get("accompaniment") else None
    if stems.get("provenance"):
        diagnosis["stemProvenance"] = stems["provenance"]
    result = {**rhythm, "diagnostics": diagnosis, "percussion": percussion}
    if context_percussion is not None:
        result["contextPercussion"] = context_percussion
    if hit_timing is not None:
        result["hitTiming"] = hit_timing
    result["findings"] = [*rhythm.get("findings", []), *diagnosis["findings"]]
    if diagnosis["status"] == "needs_review":
        result["status"] = "needs_review"
    elif diagnosis["status"] == "unknown" and result.get("status") == "observed":
        result["status"] = "unknown"
    return result


def inspect_audio_rhythm(audio: Path | str, *, requested_bpm: float | None = None,
                         time_signature: str | int | None = None, style_prompt: str = "",
                         separation: dict[str, Any] | None = None,
                         stem_inputs: dict[str, Any] | None = None,
                         expected_sections: list[dict[str, Any]] | None = None,
                         intent_snapshot: RhythmIntent | None = None,
                         diagnostics_version: str = DIAGNOSTICS_VERSION) -> dict[str, Any]:
    """Run read-only DSP and hash verification; this never launches a model."""
    audio = Path(audio).expanduser().resolve()
    source_hash = sha256_file(audio)
    if diagnostics_version not in SUPPORTED_VERSIONS:
        raise ValueError("Unsupported rhythm diagnostics version")
    if intent_snapshot is not None:
        if expected_sections is not None:
            raise ValueError("Provide either expected sections or an intent snapshot")
        if intent_snapshot.source_artifact_sha256 != source_hash:
            raise ValueError("Rhythm intent source hash does not match the target audio")
        expected_sections = [dict(section) for section in intent_snapshot.sections]
    if expected_sections is not None:
        with wave.open(str(audio), "rb") as stream:
            expected_sections = validate_expected_sections(expected_sections, stream.getnframes() / stream.getframerate())
    rhythm = analyze_music_structure(audio, requested_bpm=requested_bpm, time_signature=time_signature, style_prompt=style_prompt)
    rhythm["measuredArtifactSha256"] = source_hash
    result = attach_diagnostics(rhythm, audio, duration_seconds=rhythm["durationSeconds"],
        source_sha256=source_hash, style_prompt=style_prompt, separation=separation, stem_inputs=stem_inputs,
        expected_sections=expected_sections, diagnostics_version=diagnostics_version)
    if intent_snapshot is not None:
        result["intentSnapshot"] = intent_snapshot.to_dict()
        result["intentFileSha256"] = intent_snapshot.intent_file_sha256
    if sha256_file(audio) != source_hash:
        raise ValueError("Audio changed during read-only rhythm inspection")
    return result


def inspect_saved_candidate(project_root: Path | str, *, candidate_id: str,
                            cache_path: Path | str | None = None, stem_label: str | None = None,
                            evidence_candidate_id: str | None = None,
                            intent_path: Path | str | None = None) -> dict[str, Any]:
    """Append a fresh analysis artifact without changing prior QC/audio/review."""
    store = ProjectStore(project_root)
    with store.generation_lock():
        project = store.load()
        candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
        artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
        valid, reason = store.verify_artifact(artifact)
        if not valid:
            raise ValueError(reason)
        audio = store.resolve_artifact(artifact)
        request = store.find_by_id(project, "requests", "requestId", candidate["requestId"])["parameters"]
        intent = load_rhythm_intent(intent_path, audio=audio, source_sha256=artifact["sha256"]) if intent_path is not None else None
        stems, cache_hash = None, None
        if cache_path is not None:
            reference_candidate = store.find_by_id(project, "candidates", "candidateId", evidence_candidate_id or candidate_id)
            reference = store.find_by_id(project, "artifacts", "artifactId", reference_candidate["artifactId"])
            valid, reason = store.verify_artifact(reference)
            if not valid:
                raise ValueError(reason)
            stems = load_cached_stem_evidence(cache_path, audio=audio, label=stem_label,
                evidence_audio=store.resolve_artifact(reference))
            cache_hash = stems["provenance"]["cacheSha256"]
        elif evidence_candidate_id is not None or stem_label is not None:
            raise ValueError("Evidence source/label requires an explicit stem cache")
        with store.transaction() as current:
            parameters = {"inspectionKind": "rhythm-diagnostics",
                "candidateId": candidate_id, "sourceArtifactId": artifact["artifactId"],
                "sourceArtifactSha256": artifact["sha256"], "diagnosticsVersion": DIAGNOSTICS_VERSION,
                "percussionVersion": PERCUSSION_VERSION, "hitTimingVersion": HIT_TIMING_VERSION,
                "stemCacheSha256": cache_hash,
                "evidenceCandidateId": evidence_candidate_id, "newModelInference": False}
            if intent is not None:
                parameters.update(intentSnapshot=intent.to_dict(), intentFileSha256=intent.intent_file_sha256)
            job = store.append_job(current, kind="quality-check", parameters=parameters)
            store.transition_job(job, "running", stage="quality_rhythm")
        report_path = store.root / "artifacts" / "analysis" / f"{job['jobId']}.json"
        try:
            separation = candidate.get("quality", {}).get("observation", {}).get("separation")
            rhythm = inspect_audio_rhythm(audio, requested_bpm=request.get("bpm"), time_signature=request.get("time_signature"),
                style_prompt=request.get("sourceStylePrompt", request.get("prompt", "")), separation=separation, stem_inputs=stems,
                intent_snapshot=intent)
            if rhythm["measuredArtifactSha256"] != artifact["sha256"] or not store.verify_artifact(artifact)[0]:
                raise ValueError("Candidate audio changed during rhythm inspection")
            inspection_id = new_id("inspection")
            report = {"inspectionId": inspection_id, "candidateId": candidate_id,
                "sourceArtifactId": artifact["artifactId"], "sourceArtifactSha256": artifact["sha256"],
                "createdAt": utc_now(), "newModelInference": False, "rhythm": rhythm}
            if intent is not None:
                report.update(intentSnapshot=intent.to_dict(), intentFileSha256=intent.intent_file_sha256)
            if report_path.exists():
                raise ValueError("A new rhythm inspection cannot replace an existing report")
            atomic_write_json(report_path, report)
            report_artifact = {"artifactId": new_id("artifact"), "kind": "rhythm-analysis", "path": store.relative_path(report_path),
                "bytes": report_path.stat().st_size, "sha256": sha256_file(report_path),
                "sourceArtifactId": artifact["artifactId"], "sourceArtifactSha256": artifact["sha256"],
                "createdByJobId": job["jobId"], "createdAt": utc_now()}
            inspection = {"inspectionId": inspection_id, "candidateId": candidate_id, "artifactId": report_artifact["artifactId"],
                "sourceArtifactId": artifact["artifactId"], "sourceArtifactSha256": artifact["sha256"],
                "createdByJobId": job["jobId"], "createdAt": report["createdAt"]}
            with store.transaction() as current:
                current["artifacts"].append(report_artifact)
                current.setdefault("rhythmInspections", []).append(inspection)
                version = store.find_by_id(current, "candidates", "candidateId", candidate_id)
                version.setdefault("rhythmInspectionIds", []).append(inspection_id)
                current["revisions"].append({"revisionId": new_id("revision"), "kind": "rhythm-inspection",
                    "previousRevisionId": current["revisions"][-1]["revisionId"] if current["revisions"] else None,
                    "before": {"candidateId": candidate_id, "inspectionIds": version["rhythmInspectionIds"][:-1]},
                    "after": {"candidateId": candidate_id, "inspectionId": inspection_id, "artifactId": report_artifact["artifactId"]},
                    "createdAt": utc_now()})
                saved_job = store.find_by_id(current, "jobs", "jobId", job["jobId"])
                saved_job["resultRefs"] = [inspection_id, report_artifact["artifactId"]]
                store.transition_job(saved_job, "succeeded", stage="quality_checked", progress=1)
            return {**report, "reportArtifactId": report_artifact["artifactId"], "reportPath": str(report_path)}
        except (Exception, KeyboardInterrupt) as error:
            with store.transaction() as current:
                saved_job = store.find_by_id(current, "jobs", "jobId", job["jobId"])
                if saved_job["status"] == "running":
                    store.transition_job(saved_job, "cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                        stage="quality_inspection_failed", error=f"{type(error).__name__}: {error}")
            raise


def candidate_inspection_view(store: ProjectStore, project: dict[str, Any], candidate: dict[str, Any],
                              artifact: dict[str, Any], *, source_valid: bool | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Overlay the latest verified inspection while preserving saved original QC."""
    ids = set(candidate.get("rhythmInspectionIds", []))
    latest = next((item for item in reversed(project.get("rhythmInspections", [])) if item["inspectionId"] in ids), None)
    original = candidate.get("quality")
    if latest is None:
        return original, []
    quality = deepcopy(original or {"status": "unknown", "summary": "리듬 구간 검사를 마쳤어요.", "complete": True,
        "attempt": 1, "maxAttempts": 1, "preferred": True, "score": None, "retryReasons": [],
        "audio": {}, "lyrics": {"status": "unknown"}, "processing": None})
    try:
        if source_valid is False or source_valid is None and not store.verify_artifact(artifact)[0]:
            raise ValueError("Rhythm inspection source audio is no longer verified")
        report_artifact = store.find_by_id(project, "artifacts", "artifactId", latest["artifactId"])
        if latest["sourceArtifactSha256"] != artifact["sha256"] or not store.verify_artifact(report_artifact)[0]:
            raise ValueError("Rhythm inspection artifact or source hash is invalid")
        if report_artifact["bytes"] > 4_000_000:
            raise ValueError("Rhythm inspection report is too large")
        report = json.loads(store.resolve_artifact(report_artifact).read_text(encoding="utf-8"))
        if report["candidateId"] != candidate["candidateId"] or report["sourceArtifactSha256"] != artifact["sha256"]:
            raise ValueError("Rhythm inspection source identity does not match this version")
        quality["rhythm"] = report["rhythm"]
        quality["inspectionId"] = latest["inspectionId"]
        quality["inspectionScope"] = "rhythm_only_additional_observation"
        if quality["rhythm"]["status"] == "needs_review":
            quality.update(status="attention", summary="추가한 리듬 검사에서 확인할 구간이 있어요. 직접 들어 확인해 주세요.")
        elif quality["rhythm"]["status"] == "unknown" and quality["status"] == "passed":
            quality.update(status="unknown", summary="추가 리듬 검사 일부는 판단하기 어려워요.")
        findings = [{**finding, "findingId": f"{latest['inspectionId']}:{index}", "artifactId": artifact["artifactId"],
                     "createdAt": latest["createdAt"]} for index, finding in enumerate(quality["rhythm"].get("findings", []))]
        return quality, findings
    except (ValueError, OSError, KeyError, TypeError) as error:
        quality.update(status="unknown", summary="추가 리듬 검사 보고서를 검증하지 못했어요.", inspectionError=str(error))
        quality["rhythm"] = {"status": "unknown", "requestedBpm": None, "estimatedBpm": None, "confidence": 0,
            "findings": [], "error": str(error)}
        return quality, []
