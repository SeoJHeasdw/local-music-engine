"""Historical, opt-in ACE comparisons; start the legacy server separately.

Each language/seed pair changes only the two additional production rules. Every
attempt gets a fresh project and one inference opportunity. Audio, requests and
automatic observations remain available even when another attempt fails. This
runner never starts a server, resumes/reuses output, or gives a music rating.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
import unicodedata
from typing import Any, Callable

from local_music_engine.ace_adapter import AceStepClient
from local_music_engine.auto_quality import quality_policy
from local_music_engine.jobs import validate_seed
from local_music_engine.production_rules import catalog, normalize_selection
from local_music_engine.storage import ProjectStore, atomic_write_json, utc_now
from local_music_engine.workflow import export_selected, generate_candidates


SIX_RULES = (
    "steady-groove", "repeated-harmony", "focused-arrangement", "clear-vocal",
    "clean-production", "simple-structure",
)
EXTRA_RULES = ("section-development", "phrase-breathing")
# Generic fixed experiment inputs; personal/song-specific lyrics can be supplied
# from local files and are saved only below the requested runtime directory.
DEFAULT_LYRICS = {
    "en": "[Verse]\nCity lights are fading\nYour voice stays with me\nI walk through the silence\nAnd let the night breathe\n\n[Chorus]\nStay until the morning\nLet the cold wind go\nStep into the daylight\nWe can take it slow",
    "ko": "[Verse]\n불빛이 멀어져\n네 목소리 남아\n조용히 걸으며\n긴 밤을 보내네\n\n[Chorus]\n아침이 올 때면\n찬 바람 놓아줘\n빛으로 걸어가\n천천히 가도 돼",
}
CONTROL_KEYS = (
    "sungText", "audio_duration", "vocal_language", "model", "lm_model_path",
    "lm_backend", "thinking", "inference_steps", "guidance_scale", "bpm",
    "key_scale", "time_signature", "seed", "use_random_seed", "batch_size",
    "task_type", "lm_temperature", "use_cot_caption", "use_cot_language",
    "constrained_decoding", "audio_format",
)


@dataclass(frozen=True)
class ComparisonConfig:
    output_dir: Path
    languages: tuple[str, ...] = ("en", "ko")
    seeds: tuple[int, ...] = (202610071, 202610072)
    base_url: str = "http://127.0.0.1:18001"
    duration_seconds: float = 30.0
    bpm: int = 84
    key_scale: str = "A minor"
    time_signature: str = "4"
    style_prompt: str = "intimate male melodic rap, mellow late-night mood"
    model: str = "acestep-v15-turbo"
    lm_model: str = "acestep-5Hz-lm-4B"
    lm_temperature: float = 0.6
    include_baseline: bool = False
    timeout_seconds: float = 1800.0


def experiment_profiles(include_baseline: bool = False) -> list[dict[str, Any]]:
    profiles = [
        {"id": "successful-six", "selection": {"version": 1, "presetId": "emotional-hiphop", "ruleIds": list(SIX_RULES)}},
        {"id": "enhanced-eight", "selection": {"version": 1, "presetId": "emotional-hiphop", "ruleIds": [*SIX_RULES, *EXTRA_RULES]}},
    ]
    if include_baseline:
        profiles.insert(0, {"id": "no-rules", "selection": {"version": 1, "presetId": None, "ruleIds": []}})
    for profile in profiles:
        profile["selection"] = normalize_selection(profile["selection"])
    return profiles


def _validate_config(config: ComparisonConfig, lyrics: dict[str, str]) -> Path:
    output = config.output_dir.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("comparison output already exists; choose a new directory")
    if not config.languages or len(set(config.languages)) != len(config.languages) or any(language not in {"en", "ko"} for language in config.languages):
        raise ValueError("languages must be distinct en/ko values")
    if not 1 <= len(config.seeds) <= 3 or len(set(config.seeds)) != len(config.seeds):
        raise ValueError("choose one to three distinct paired seeds")
    for seed in config.seeds:
        validate_seed(seed)
    if not math.isfinite(config.duration_seconds) or not 10 <= config.duration_seconds <= 600:
        raise ValueError("duration must be between 10 and 600 seconds")
    if type(config.bpm) is not int or not 30 <= config.bpm <= 300:
        raise ValueError("bpm must be an integer between 30 and 300")
    if not math.isfinite(config.lm_temperature) or not 0 < config.lm_temperature <= 2:
        raise ValueError("LM temperature must be greater than zero and at most two")
    if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    for language in config.languages:
        if not isinstance(lyrics.get(language), str) or not lyrics[language].strip():
            raise ValueError(f"provide fixed manual lyrics for {language}")
    return output


def controlled_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Conditions expected to be identical within a language/seed pair."""

    # Section annotations and phrase line breaks are part of the selected rule
    # intervention. They may change; actual sung words/order may not.
    return {key: sung_text(parameters.get("lyrics", "")) if key == "sungText"
            else deepcopy(parameters.get(key)) for key in CONTROL_KEYS}


def sung_text(lyrics: str) -> str:
    text = re.sub(r"\[[^\]\n]+\]", " ", unicodedata.normalize("NFC", lyrics))
    return " ".join(text.split())


def validate_initial_engine_health(health: dict[str, Any], config: ComparisonConfig) -> None:
    """The pinned server may intentionally unload its LM after a cover request."""

    unloaded_lm = health.get("loaded_lm_model") is None and health.get("llm_initialized") is False
    if health.get("loaded_model") != config.model or (health.get("loaded_lm_model") != config.lm_model and not unloaded_lm):
        raise RuntimeError("the already-running server must have the requested DiT and LM loaded, or an explicitly uninitialized LM")


def validate_post_generation_health(health: dict[str, Any], config: ComparisonConfig) -> None:
    """Successful thinking-mode requests must finish with the intended LM resident."""

    if (health.get("loaded_model") != config.model or health.get("loaded_lm_model") != config.lm_model
            or health.get("llm_initialized") is False or health.get("models_initialized") is False):
        raise RuntimeError("post-generation engine health does not confirm the requested DiT and LM")


def attempt_evidence(store: ProjectStore, result: dict[str, Any]) -> dict[str, Any]:
    project = store.load()
    requests = [deepcopy(request) for request in project["requests"] if request["adapter"] == "ace-step-rest"]
    jobs = [job for job in project["jobs"] if job["kind"] == "generate-candidate"]
    artifacts = []
    for artifact in project["artifacts"]:
        valid, reason = store.verify_artifact(artifact)
        artifacts.append({**deepcopy(artifact), "absolutePath": str(store.resolve_artifact(artifact)), "valid": valid, "validation": reason})
    return {
        "generationResult": deepcopy(result), "actualEngineRequests": requests,
        "generationAttempts": len(jobs),
        "submissionEvidence": [{"jobId": job["jobId"], "status": job["status"],
                                "remoteTaskId": job.get("remoteTaskId")} for job in jobs],
        "selectedCandidateId": project["selectedCandidateId"],
        "candidates": [{"candidateId": candidate["candidateId"], "artifactId": candidate["artifactId"],
                        "parentCandidateId": candidate.get("parentCandidateId"),
                        "humanReview": deepcopy(candidate["humanReview"]),
                        "automaticQuality": deepcopy(candidate.get("quality"))} for candidate in project["candidates"]],
        "artifacts": artifacts,
        "allArtifactsVerified": all(artifact["valid"] for artifact in artifacts),
        "humanListening": "unreviewed",
        "musicQuality": {"status": "unknown", "scope": "automatic measurements and transcript matching only",
                         "reason": "PCM, ASR and timing observations do not establish musical coherence, rhythm quality or preference."},
    }


def _assert_fresh_attempt(evidence: dict[str, Any], config: ComparisonConfig, language: str, seed: int) -> None:
    result = evidence["generationResult"]
    if result.get("reusedCandidateIds"):
        raise RuntimeError("controlled comparisons must not reuse an earlier result")
    if evidence["generationAttempts"] > 1 or len(evidence["actualEngineRequests"]) > 1:
        raise RuntimeError("controlled attempt exceeded its one-inference budget")
    if not evidence["allArtifactsVerified"]:
        raise RuntimeError("comparison artifact verification failed")
    if evidence["selectedCandidateId"] is not None or any(candidate["humanReview"]["status"] != "unreviewed" for candidate in evidence["candidates"]):
        raise RuntimeError("automatic comparison must not change human review/selection")
    for request in evidence["actualEngineRequests"]:
        parameters = request["parameters"]
        expected = {"seed": seed, "audio_duration": config.duration_seconds, "bpm": config.bpm,
                    "key_scale": config.key_scale, "time_signature": config.time_signature,
                    "vocal_language": language, "model": config.model, "lm_model_path": config.lm_model,
                    "lm_temperature": config.lm_temperature}
        for key, value in expected.items():
            if parameters.get(key) != value:
                raise RuntimeError(f"controlled input changed: {key} expected {value!r}, got {parameters.get(key)!r}")


def run_comparison(
    config: ComparisonConfig, *, lyrics: dict[str, str] | None = None,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
    quality_backend: Any | None = None, execution_mode: str = "test-double",
) -> dict[str, Any]:
    """Run fresh projects; the command entry point explicitly records real mode."""

    supplied_lyrics = DEFAULT_LYRICS if lyrics is None else lyrics
    output = _validate_config(config, supplied_lyrics)
    fixed_lyrics = {language: supplied_lyrics[language] for language in config.languages}
    profiles = experiment_profiles(config.include_baseline)
    health = client_factory(config.base_url).health()
    validate_initial_engine_health(health, config)
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "report.json"
    report: dict[str, Any] = {
        "schemaVersion": 1, "executionMode": execution_mode, "createdAt": utc_now(), "status": "running",
        "engine": health, "catalogSnapshot": catalog(engine="ace-step"), "profiles": profiles,
        "fixedConditions": {"languages": list(config.languages), "seeds": list(config.seeds),
            "lyricsByLanguage": fixed_lyrics, "lyricsSha256ByLanguage": {key: hashlib.sha256(value.encode()).hexdigest() for key, value in fixed_lyrics.items()},
            "lyricControl": "same manual lyrics and sung words/order; section annotations and line breaks belong to the rule intervention",
            "durationSeconds": config.duration_seconds, "bpm": config.bpm, "keyScale": config.key_scale,
            "timeSignature": config.time_signature, "baseStylePrompt": config.style_prompt,
            "model": config.model, "lmModel": config.lm_model, "lmTemperature": config.lm_temperature,
            "maximumInferenceAttemptsPerCondition": 1},
        "changedFactor": "selected production rules (plus preset removal for optional no-rules baseline)",
        "attempts": [], "pairedComparisons": [], "humanListening": "unreviewed",
        "musicQuality": {"status": "unknown", "automaticWinner": None,
                         "reason": "Listen to paired outputs; automatic checks do not select a musical winner."},
    }

    def save() -> None:
        report["updatedAt"] = utc_now()
        atomic_write_json(report_path, report)

    save()
    for language in config.languages:
        for seed in config.seeds:
            pair = {"language": language, "seed": seed, "attemptIds": [], "controlsMatch": None, "differences": []}
            report["pairedComparisons"].append(pair)
            reference_controls = None
            for profile in profiles:
                attempt_id = f"{language}-{seed}-{profile['id']}"
                project_root = output / "projects" / attempt_id
                attempt: dict[str, Any] = {"attemptId": attempt_id, "language": language, "seed": seed,
                    "profileId": profile["id"], "projectPath": str(project_root), "status": "running", "startedAt": utc_now()}
                report["attempts"].append(attempt)
                pair["attemptIds"].append(attempt_id)
                save()
                started = time.monotonic()
                store = None
                try:
                    store = ProjectStore.initialize(project_root, title=f"Production comparison · {attempt_id}",
                        lyrics=fixed_lyrics[language], style_prompt=config.style_prompt,
                        target_duration_seconds=config.duration_seconds, bpm=config.bpm, key_scale=config.key_scale,
                        time_signature=config.time_signature, production_rules=profile["selection"])
                    result = generate_candidates(project_root, seeds=[seed], base_url=config.base_url, engine="ace-step",
                        model=config.model, lm_model=config.lm_model, lm_temperature=config.lm_temperature,
                        timeout_seconds=config.timeout_seconds, client_factory=client_factory,
                        quality=quality_policy("auto", max_attempts=1), quality_backend=quality_backend)
                    evidence = attempt_evidence(store, result)
                    attempt.update(evidence)
                    post_health = client_factory(config.base_url).health()
                    attempt["postGenerationEngineHealth"] = post_health
                    attempt["postGenerationEngineHealthMatches"] = False
                    if result["status"] == "succeeded":
                        validate_post_generation_health(post_health, config)
                        attempt["postGenerationEngineHealthMatches"] = True
                    _assert_fresh_attempt(evidence, config, language, seed)
                    if store.load()["inputs"]["lyricsOriginal"] != fixed_lyrics[language]:
                        raise RuntimeError("manual lyric source changed during comparison")
                    if evidence["actualEngineRequests"]:
                        controls = controlled_parameters(evidence["actualEngineRequests"][0]["parameters"])
                        if controls["sungText"] != sung_text(fixed_lyrics[language]):
                            raise RuntimeError("rule intervention changed sung words or their order")
                        if reference_controls is None:
                            reference_controls = controls
                            pair["controlsMatch"] = True
                        elif controls != reference_controls:
                            pair["controlsMatch"] = False
                            pair["differences"] = [key for key in CONTROL_KEYS if controls[key] != reference_controls[key]]
                            raise RuntimeError("paired requests changed conditions besides production rules: " + ", ".join(pair["differences"]))
                    if result["status"] == "succeeded" and result.get("candidateIds"):
                        candidate_id = result.get("recommendedCandidateId") or result["candidateIds"][0]
                        exported = export_selected(project_root, candidate_id=candidate_id, output=output / "audio" / f"{attempt_id}.wav")
                        attempt.update(attempt_evidence(store, result))
                        attempt["export"] = exported
                    attempt["status"] = result["status"]
                    if result["status"] in {"cancelled", "interrupted"}:
                        report["status"] = "interrupted"
                except KeyboardInterrupt:
                    attempt["status"] = "interrupted"
                    report["status"] = "interrupted"
                    if store is not None:
                        try:
                            attempt.update(attempt_evidence(store, {"status": "interrupted"}))
                        except Exception as error:
                            attempt["evidenceError"] = f"{type(error).__name__}: {error}"
                    raise
                except Exception as error:
                    attempt["status"] = "failed"
                    attempt["error"] = f"{type(error).__name__}: {error}"
                    if store is not None and "generationResult" not in attempt:
                        try:
                            attempt.update(attempt_evidence(store, {"status": "failed"}))
                        except Exception as evidence_error:
                            attempt["evidenceError"] = f"{type(evidence_error).__name__}: {evidence_error}"
                finally:
                    attempt["elapsedSeconds"] = round(time.monotonic() - started, 3)
                    attempt["completedAt"] = utc_now()
                    save()
                print(json.dumps({"attemptId": attempt_id, "status": attempt["status"], "reportPath": str(report_path)}, ensure_ascii=False), flush=True)
                if report["status"] == "interrupted":
                    return report
    report["status"] = "completed" if all(attempt["status"] == "succeeded" for attempt in report["attempts"]) else "completed_with_failures"
    report["completedAt"] = utc_now()
    save()
    return report


def _csv_languages(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _csv_seeds(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part.strip()) for part in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("seeds must be comma-separated integers") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--languages", type=_csv_languages, default=("en", "ko"))
    parser.add_argument("--seeds", type=_csv_seeds, default=(202610071, 202610072))
    parser.add_argument("--include-baseline", action="store_true")
    parser.add_argument("--lyrics-en-file", type=Path)
    parser.add_argument("--lyrics-ko-file", type=Path)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--bpm", type=int, default=84)
    parser.add_argument("--key", default="A minor")
    parser.add_argument("--model", default="acestep-v15-turbo")
    parser.add_argument("--lm-model", default="acestep-5Hz-lm-4B")
    parser.add_argument("--lm-temperature", type=float, default=0.6)
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args()
    lyrics = dict(DEFAULT_LYRICS)
    for language, file in (("en", args.lyrics_en_file), ("ko", args.lyrics_ko_file)):
        if file is not None:
            lyrics[language] = file.read_text(encoding="utf-8")
    config = ComparisonConfig(output_dir=args.output_dir, languages=args.languages, seeds=args.seeds,
        base_url=args.base_url, duration_seconds=args.duration, bpm=args.bpm, key_scale=args.key,
        model=args.model, lm_model=args.lm_model, lm_temperature=args.lm_temperature,
        include_baseline=args.include_baseline, timeout_seconds=args.timeout)
    report = run_comparison(config, lyrics=lyrics, execution_mode="real-engine")
    print(json.dumps({"reportPath": str(args.output_dir.expanduser().absolute() / "report.json"),
                      "status": report["status"], "attempts": len(report["attempts"]),
                      "musicQuality": report["musicQuality"]}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
