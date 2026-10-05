"""Compare fresh Music 3 candidates against an explicitly supplied local benchmark.

The benchmark is a verified playback import, never model conditioning. All real
attempts, failed observations and the pending human verdict remain inspectable.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import shutil
import time
import json
import math
import os
import re
import unicodedata
from typing import Any, Callable

from local_music_engine.auto_quality import quality_policy
from local_music_engine.music3_adapter import Music3Client
from local_music_engine.music3 import BASE_URL, ENGINE, MAX_DURATION_SECONDS, MODEL
from local_music_engine.jobs import validate_seed
from local_music_engine.production_rules import catalog
from local_music_engine.qc import artifact_and_findings
from local_music_engine.storage import ProjectStore, atomic_write_json, fingerprint, new_id, utc_now
from local_music_engine.workflow import export_selected, generate_candidates, revise_inputs


def sung_text(lyrics: str) -> str:
    """Section directions and whitespace may differ; sung words/order may not."""
    text = re.sub(r"\[[^\]\r\n]+\]", " ", unicodedata.normalize("NFC", lyrics))
    return " ".join(text.split())


def _fresh_directory(value: Path) -> Path:
    path = value.expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise FileExistsError("use fresh comparison output and project directories")
    return path.resolve()


def _attempt_evidence(store: ProjectStore, result: dict[str, Any]) -> dict[str, Any]:
    project = store.load()
    batch_id = result.get("batchJobId")
    if batch_id is None:
        batches = [job for job in project["jobs"] if job["kind"] == "candidate-batch"]
        batch_id = batches[-1]["jobId"] if batches else None
    jobs = [job for job in project["jobs"] if job["kind"] == "generate-candidate"
            and job.get("parentJobId") == batch_id]
    requests = [deepcopy(store.find_by_id(project, "requests", "requestId", job["parameters"]["requestId"]))
                for job in jobs]
    return {"actualEngineRequests": requests, "generationAttempts": len(jobs),
            "submissionEvidence": [{"jobId": job["jobId"], "status": job["status"],
                                    "remoteTaskId": job.get("remoteTaskId")} for job in jobs]}


def _assert_controlled(evidence: dict[str, Any], *, lyrics: str,
                       parameters: dict[str, Any], real: bool) -> None:
    if (evidence["generationAttempts"] != 1 or evidence["submissionEvidence"][0]["status"] != "succeeded"
            or not evidence["submissionEvidence"][0]["remoteTaskId"]):
        raise RuntimeError("the comparison must submit exactly one generation per recipe")
    request = evidence["actualEngineRequests"][0]
    if real and request.get("executionKind") != "real-inference":
        raise RuntimeError("the frozen request does not confirm real Music 3 inference")
    frozen = request["parameters"]
    if frozen.get("engine") != ENGINE or frozen.get("task_type") != "text2music":
        raise RuntimeError("comparison must use independent Music 3 text-to-music generation")
    for key in ("source_artifact_sha256", "reference_audio", "src_audio", "coverSource"):
        if frozen.get(key):
            raise RuntimeError("benchmark import must never become source conditioning")
    if sung_text(frozen["lyrics"]) != sung_text(lyrics):
        raise RuntimeError("controlled input changed: sung words or their order")
    for key in ("audio_duration", "seed", "bpm", "key_scale", "time_signature"):
        expected = parameters.get(key)
        if expected not in (None, "") and frozen.get(key) != expected:
            raise RuntimeError(f"controlled input changed: {key}")


def import_benchmark(source: ProjectStore, candidate_id: str, destination: ProjectStore) -> str:
    project = source.load()
    candidate = source.find_by_id(project, "candidates", "candidateId", candidate_id)
    if candidate.get("status") != "ready":
        raise ValueError("benchmark candidate must be ready")
    artifact = source.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
    valid, reason = source.verify_artifact(artifact)
    if not valid:
        raise ValueError(reason)
    original = source.find_by_id(project, "requests", "requestId", candidate["requestId"])
    target = destination.root / "artifacts" / "benchmarks" / f"{new_id('benchmark')}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    relative = destination.relative_path(target)
    # The project is fresh and the destination name unique. No original is edited.
    owned = False
    try:
        with target.open("xb") as handle:
            owned = True
            with source.resolve_artifact(artifact).open("rb") as reader:
                shutil.copyfileobj(reader, handle)
            handle.flush()
            os.fsync(handle.fileno())
        imported, findings = artifact_and_findings(path=target, project_relative_path=relative,
            artifact_kind="benchmark-playback-audio", created_by_job_id=None,
            requested_duration_seconds=artifact["audio"]["durationSeconds"])
        if (imported["sha256"], imported["bytes"]) != (artifact["sha256"], artifact["bytes"]):
            raise ValueError("benchmark changed during explicit import")
    except (Exception, KeyboardInterrupt):
        if owned:
            target.unlink(missing_ok=True)
        raise
    provenance = {"projectId": project["projectId"], "candidateId": candidate_id,
        "artifactId": artifact["artifactId"], "sha256": artifact["sha256"], "bytes": artifact["bytes"],
        "requestId": original["requestId"], "requestFingerprint": original["fingerprint"],
        "playbackOnly": True, "sourceConditioning": False}
    request = {**deepcopy(original), "requestId": new_id("request"),
        "adapter": "verified-audio-import", "adapterVersion": "benchmark-import-v1",
        "executionKind": "verified-import", "createdAt": utc_now(),
        "fingerprint": fingerprint(provenance), "sourceImport": provenance,
        "originalRequest": deepcopy(original)}
    copied = {**deepcopy(candidate), "candidateId": new_id("candidate"),
        "requestId": request["requestId"], "artifactId": imported["artifactId"],
        "parentCandidateId": None, "editRange": None, "contextRange": None,
        "findingIds": [item["findingId"] for item in findings], "createdAt": utc_now(),
        "sourceImport": provenance}
    with destination.transaction() as fresh:
        fresh["requests"].append(request)
        fresh["artifacts"].append(imported)
        fresh["findings"].extend(findings)
        fresh["candidates"].append(copied)
    return copied["candidateId"]


def run_comparison(args: argparse.Namespace, *, client_factory: Callable[..., Any] = Music3Client,
                   quality_backend: Any | None = None, execution_mode: str = "real-engine") -> dict[str, Any]:
    """The CLI always uses real mode; dependency injection is for contract tests."""
    recipes = args.recipes.split(",")
    if not recipes or len(set(recipes)) != len(recipes) or any(item not in {"native", "concise", "compact"} for item in recipes):
        raise ValueError("choose distinct native,concise,compact recipes")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if execution_mode not in {"real-engine", "test-double"}:
        raise ValueError("unknown comparison execution mode")
    output, root = _fresh_directory(args.output_dir), _fresh_directory(args.project_dir)
    if root == output or root in output.parents or output in root.parents:
        raise ValueError("comparison output and project directories must be separate")
    source = ProjectStore(args.baseline_project)
    original = source.load()
    baseline = source.find_by_id(original, "candidates", "candidateId", args.baseline_candidate)
    parameters = source.find_by_id(original, "requests", "requestId", baseline["requestId"])["parameters"]
    lyrics = parameters.get("songPlan", {}).get("lyricsOriginal", parameters.get("qualityPreparation", {}).get("lyricsOriginal", parameters.get("sourceLyricsOriginal", parameters["lyrics"])))
    rules = parameters.get("productionRules", {})
    style = rules.get("baseStylePrompt", parameters.get("sourceStylePrompt", parameters["prompt"]))
    duration = parameters["audio_duration"]
    seed = parameters["seed"]
    validate_seed(seed)
    if not math.isfinite(duration) or not 10 <= duration <= MAX_DURATION_SECONDS:
        raise ValueError("benchmark requested duration is outside the Music 3 limit")
    bpm = parameters.get("bpm")
    if bpm is not None:
        if isinstance(bpm, bool) or not isinstance(bpm, (int, float)) or not math.isfinite(bpm) or not float(bpm).is_integer():
            raise ValueError("benchmark BPM must be an integer tempo")
        bpm = int(bpm)
    source_artifact = source.find_by_id(original, "artifacts", "artifactId", baseline["artifactId"])
    valid, reason = source.verify_artifact(source_artifact)
    if baseline.get("status") != "ready" or not valid:
        raise ValueError("benchmark candidate must be ready and verified: " + reason)
    health = client_factory(args.base_url).health()
    if (execution_mode == "real-engine" and health.get("realInference") is not True):
        raise RuntimeError("comparison requires the actual weight-backed Music 3 server")
    if health.get("engine") != ENGINE or health.get("models_initialized") is not True or health.get("loaded_model") != MODEL:
        raise RuntimeError("comparison requires the requested initialized Music 3 server")
    preset = next(item for item in catalog()["presets"] if item["id"] == "emotional-hiphop")
    selection = {"version": 1, "presetId": preset["id"],
                 "ruleIds": [*preset["ruleIds"], "section-development", "phrase-breathing", "melodic-hook", "expressive-performance"]}
    root.mkdir(parents=True, exist_ok=False)
    store = ProjectStore.initialize(root, title="기준 곡과 Music 3 비교", lyrics=lyrics,
        style_prompt=style, target_duration_seconds=duration,
        bpm=bpm, key_scale=parameters.get("key_scale"),
        time_signature=parameters.get("time_signature"), production_rules=selection)
    baseline_id = import_benchmark(source, args.baseline_candidate, store)
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "report.json"
    report = {"status": "running", "executionMode": execution_mode, "createdAt": utc_now(), "projectPath": str(root),
        "benchmarkCandidateId": baseline_id, "engineBefore": health, "recipes": [],
        "scope": "matched words, requested duration, BPM, key and seed; independent engines and captions",
        "fixedConditions": {"lyricsOriginal": lyrics, "sungText": sung_text(lyrics),
                            "sungLineCount": sum(bool(sung_text(line)) for line in lyrics.splitlines()),
                            "durationSeconds": duration, "bpm": bpm, "keyScale": parameters.get("key_scale"),
                            "timeSignature": parameters.get("time_signature"), "seed": seed,
                            "maximumInferenceAttemptsPerRecipe": 1},
        "sourceConditioning": False, "musicalMinimumVerdict": "pending_human_listening"}
    atomic_write_json(report_path, report)
    failed = False
    for recipe in recipes:
        row = {"recipe": recipe, "status": "generating", "seed": seed, "createdAt": utc_now()}
        report["recipes"].append(row)
        atomic_write_json(report_path, report)
        started = time.monotonic()
        try:
            recipe_selection = {"version": 1, "presetId": None, "ruleIds": []} if recipe in {"concise", "compact"} else selection
            if store.load()["inputs"].get("productionRules") != recipe_selection:
                revise_inputs(root, production_rules=recipe_selection)
            if recipe == "concise":
                effective_style = ("Emotional melodic hip hop, laid-back boom bap, warm repeating piano motif, round bass, intimate warm male lead. "
                    "Wistful late-night verse with relaxed melodic rap; soulful fully sung chorus with a memorable rising-and-falling hook, "
                    "sustained line endings and gentle harmony lift. Tender longing becomes quiet hope. "
                    "Polished balanced studio sound, natural dynamics, resolved piano ending.")
            elif recipe == "compact":
                effective_style = ("Emotional hip hop, warm piano loop, rounded bass and steady boom-bap drums, intimate male lead. "
                    "Forward-flowing rhythmic melodic rap in the verse, compact one-bar vocal phrases with a steady pocket; "
                    "a catchy soulful sung chorus with a clear lift and resolution. "
                    "The lead enters promptly after a brief pickup; four short verse lines flow directly into four chorus lines, "
                    "then a brief clean ending. Wistful warmth and quiet hope, balanced studio sound.")
            else:
                effective_style = style
            result = generate_candidates(root, seeds=[seed], base_url=args.base_url, engine="minimax-music3",
                style_prompt=effective_style, quality=quality_policy("auto", max_attempts=1), timeout_seconds=args.timeout,
                client_factory=client_factory, quality_backend=quality_backend)
            row.update(totalSeconds=round(time.monotonic() - started, 3), generation=result)
            evidence = _attempt_evidence(store, result)
            row.update(evidence)
            if result.get("reusedCandidateIds"):
                raise RuntimeError("comparison must generate fresh candidates instead of reusing audio")
            if not result.get("recommendedCandidateId"):
                raise RuntimeError(f"generation returned no checked candidate: {result.get('failures', result)}")
            project = store.load()
            candidate = store.find_by_id(project, "candidates", "candidateId", result["recommendedCandidateId"])
            artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
            _assert_controlled(evidence, lyrics=lyrics, parameters=parameters, real=execution_mode == "real-engine")
            if not all(store.verify_artifact(item)[0] for item in project["artifacts"]):
                raise ValueError("comparison artifact verification failed")
            generated = [item for item in project["candidates"] if not item.get("sourceImport")]
            if any(item["humanReview"]["status"] != "unreviewed" for item in generated) or project["selectedCandidateId"] is not None:
                raise ValueError("generation must not invent a human listening verdict or final selection")
            exported = export_selected(root, candidate_id=candidate["candidateId"], output=output / f"{recipe}.wav")
            row.update(status="completed", candidateId=candidate["candidateId"], sha256=artifact["sha256"],
                audio=artifact["audio"], quality=candidate["quality"], export=exported,
                realGenerationAttempts=1 if execution_mode == "real-engine" else 0,
                humanReview="unreviewed", finishedAt=utc_now())
        except (Exception, KeyboardInterrupt) as error:
            failed = True
            row.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                       error=f"{type(error).__name__}: {error}", finishedAt=utc_now(),
                       totalSeconds=round(time.monotonic() - started, 3))
            if "actualEngineRequests" not in row:
                try:
                    row.update(_attempt_evidence(store, {}))
                except Exception as evidence_error:
                    row["evidenceError"] = f"{type(evidence_error).__name__}: {evidence_error}"
        atomic_write_json(report_path, report)
        if row["status"] != "completed":
            # A timeout may leave remote computation alive. Never queue another recipe.
            break
    report.update(status="interrupted" if report["recipes"][-1]["status"] == "interrupted" else "partial" if failed else "completed", finishedAt=utc_now(),
        unsubmittedRecipes=recipes[len(report["recipes"]):])
    try:
        report["engineAfter"] = client_factory(args.base_url).health()
    except Exception as error:
        report["engineAfter"] = {"error": f"{type(error).__name__}: {error}"}
        report["status"] = "partial"
        failed = True
    atomic_write_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-project", type=Path, required=True)
    parser.add_argument("--baseline-candidate", required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--recipes", default="native,concise")
    args = parser.parse_args()
    report = run_comparison(args)
    report_path = args.output_dir.expanduser().resolve() / "report.json"
    print(json.dumps({"reportPath": str(report_path), "status": report["status"],
        "recipes": [{"recipe": row["recipe"], "status": row["status"],
                     "output": row.get("export", {}).get("externalPath")} for row in report["recipes"]]}, ensure_ascii=False, indent=2))
    if report["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
