"""Command-line interface for project creation, generation, review, and export.

Every command prints one JSON document, so the desktop app and coding agents can
drive the same workflow functions a person uses from the terminal.
"""

from __future__ import annotations

import argparse
import json
import math
import secrets
import sys
from pathlib import Path
from typing import Any

from . import assistant
from .ace_runtime import managed_client_factory
from .drafting import draft_song
from .auto_quality import quality_policy
from .jobs import frozen_batch_payloads, recover_project, resume_source
from .lane import shared_lane
from .listening import master_versions
from .production_rules import catalog as production_catalog, normalize_selection
from .song_planning import prepare_song_plan, validate_song_plan_input
from .storage import ProjectStore
from .views import candidate_rows, library, project_status
from .workflow import (
    DEFAULT_LM_MODEL,
    DEFAULT_LM_TEMPERATURE,
    REPAINT_STRENGTHS,
    cover_candidates,
    export_selected,
    generate_candidates,
    repaint_candidate,
    resume_latest_batch,
    review_candidate,
    revise_inputs,
    select_candidate,
    undo_selection,
)
from .engines import (ACE_BASE_URL, ACE_CAPABILITIES, ACE_DIT_MODEL, ACE_ENGINE, DEFAULT_ENGINE, ENGINES, MAX_DURATION_SECONDS,
                      MUSIC3_ENGINE)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _parse_seeds(value: str) -> list[int]:
    try:
        seeds = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError("seeds must be comma-separated integers") from error
    if not seeds:
        raise argparse.ArgumentTypeError("at least one seed is required")
    if len(set(seeds)) != len(seeds):
        raise argparse.ArgumentTypeError("seeds must be unique within a batch")
    return seeds


def _add_seed_args(parser: argparse.ArgumentParser) -> None:
    """One new version by default; more is an explicit choice, by count or by naming each seed."""

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--versions", type=int, choices=range(1, 5), default=1,
                       help="how many new versions to make, each from a fresh random seed (default 1)")
    group.add_argument("--seeds", type=_parse_seeds, help="comma-separated seeds; one version per seed")


def _seeds(args: argparse.Namespace) -> list[int]:
    if args.seeds is not None:
        return args.seeds
    seeds: set[int] = set()
    while len(seeds) < args.versions:  # the app draws from the same range
        seeds.add(secrets.randbelow(2_147_483_646) + 1)
    return sorted(seeds)


def _parse_feedback(value: str) -> dict[str, Any]:
    try:
        feedback = json.loads(value)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError("feedback must be a JSON object") from error
    if not isinstance(feedback, dict) or not isinstance(feedback.get("text", ""), str):
        raise argparse.ArgumentTypeError("feedback must be a JSON object with a text field")
    return feedback


def _parse_song_plan(value: str) -> dict[str, Any]:
    try:
        return validate_song_plan_input(json.loads(value))
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _parse_production_rules(value: str) -> dict[str, Any]:
    try:
        return normalize_selection(json.loads(value))
    except (json.JSONDecodeError, ValueError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _text_arg(inline: str | None, file: Path | None) -> str | None:
    if file is not None:
        return file.read_text(encoding="utf-8")
    return inline


def _add_assistant_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--assistant", choices=["rules", "ollama", "openai"], default="rules")
    parser.add_argument("--assistant-url", default="http://127.0.0.1:11434")
    parser.add_argument("--assistant-model", default="")


def _llm_config(args: argparse.Namespace) -> assistant.LlmConfig | None:
    if args.assistant == "rules":
        return None
    return assistant.LlmConfig(
        base_url=args.assistant_url, model=args.assistant_model, provider=args.assistant
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music-engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    song_plan = subparsers.add_parser("song-plan", help="preview advisory section and lyric phrasing plans without inference")
    song_plan.add_argument("--input-json", type=_parse_song_plan, required=True)

    init = subparsers.add_parser("init", help="create a project")
    init.add_argument("path", type=Path)
    init.add_argument("--title", required=True)
    lyrics_group = init.add_mutually_exclusive_group(required=True)
    lyrics_group.add_argument("--lyrics")
    lyrics_group.add_argument("--lyrics-file", type=Path)
    init.add_argument("--style", required=True)
    init.add_argument("--duration", type=float, required=True)
    init.add_argument("--structure")
    init.add_argument("--bpm", type=int)
    init.add_argument("--key")
    init.add_argument("--time-signature")
    init.add_argument("--production-rules-json", type=_parse_production_rules)
    rules = subparsers.add_parser("production-rules", help="list selectable production presets and guidance")
    rules.add_argument("--engine", choices=ENGINES, default=DEFAULT_ENGINE)
    subparsers.add_parser("capabilities", help="show the default engine's supported operations")

    generate = subparsers.add_parser("generate", help="make a new version (or --versions N)")
    generate.add_argument("path", type=Path)
    generate.add_argument("--engine", choices=ENGINES, default=DEFAULT_ENGINE)
    _add_seed_args(generate)
    generate.add_argument("--base-url", help="engine server address; defaults to the chosen engine's loopback port")
    generate.add_argument("--model", help=f"defaults to {ACE_DIT_MODEL} for ACE")
    generate.add_argument("--lm-model", default=DEFAULT_LM_MODEL)
    generate.add_argument("--lm-temperature", type=float, default=DEFAULT_LM_TEMPERATURE,
                          help="ACE planner sampling temperature; unused by Music 3")
    generate.add_argument("--quality", choices=["auto", "audio", "off"], default="auto",
                          help="automatic checks and bounded regeneration; audio skips local lyric STT")
    generate.add_argument("--quality-attempts", type=int, choices=range(1, 5), default=4,
                          help="maximum total generation attempts per requested version, including the first")
    generate.add_argument("--repair-attempts", type=int, choices=range(0, 4), default=0,
                          help="repaint a stretch whose written lines were not sung, up to this many times (off by default)")
    generate.add_argument("--style", help="revise the project style prompt first")
    generate.add_argument("--lyrics", help="revise the project lyrics first")
    generate.add_argument("--bpm", type=int, help="revise bpm first; 0 clears it")
    generate.add_argument("--feedback-json", type=_parse_feedback)
    generate.add_argument("--source-candidate-id", help="start from this version's frozen inputs before applying overrides")
    generate.add_argument("--poll-seconds", type=float, default=1.0)
    generate.add_argument("--timeout-seconds", type=float, default=1800.0)

    resume = subparsers.add_parser("resume", help="explicitly resume the latest candidate batch")
    resume.add_argument("path", type=Path)
    resume.add_argument("--engine", choices=ENGINES, default=DEFAULT_ENGINE)
    resume.add_argument("--job-id", help="resume this batch instead of the latest one")
    resume.add_argument("--base-url")
    resume.add_argument("--poll-seconds", type=float, default=1.0)
    resume.add_argument("--timeout-seconds", type=float, default=1800.0)

    recover = subparsers.add_parser("recover", help="mark abandoned local generation jobs interrupted")
    recover.add_argument("path", type=Path)

    candidates = subparsers.add_parser("candidates", help="list candidates and reviews")
    candidates.add_argument("path", type=Path)

    status = subparsers.add_parser("status", help="full project view with lineage and jobs")
    status.add_argument("path", type=Path)

    songs = subparsers.add_parser("library", help="summarize projects without hashing audio")
    songs.add_argument("--dir", type=Path)
    songs.add_argument("--project", type=Path, action="append", default=[])

    select = subparsers.add_parser("select", help="select a verified candidate")
    select.add_argument("path", type=Path)
    select.add_argument("candidate_id")

    undo = subparsers.add_parser("undo-selection", help="undo the most recent active selection")
    undo.add_argument("path", type=Path)

    review = subparsers.add_parser("review", help="record a listening decision")
    review.add_argument("path", type=Path)
    review.add_argument("candidate_id")
    review.add_argument(
        "--status",
        choices=["unreviewed", "listened", "approved", "rejected"],
        required=True,
    )
    review.add_argument("--rating", type=int)
    review.add_argument("--note")

    revise = subparsers.add_parser("revise", help="edit inputs for future requests")
    revise.add_argument("path", type=Path)
    revise.add_argument("--title")
    revise.add_argument("--style")
    revise.add_argument("--lyrics")
    revise.add_argument("--lyrics-file", type=Path)
    revise.add_argument("--duration", type=float)
    revise.add_argument("--bpm", type=int, help="0 clears the stored bpm")
    revise.add_argument("--key", help="empty string clears the stored key")
    revise.add_argument("--time-signature", help="empty string clears the stored meter")
    revise.add_argument("--production-rules-json", type=_parse_production_rules)

    repaint = subparsers.add_parser("repaint", help="create a non-destructive repaint candidate")
    repaint.add_argument("path", type=Path)
    repaint.add_argument("--engine", choices=ENGINES, default=DEFAULT_ENGINE)
    repaint.add_argument("--start", type=float, required=True)
    repaint.add_argument("--end", type=float, required=True)
    repaint.add_argument("--seed", type=int, required=True)
    repaint.add_argument("--style", help="caption for this repaint only")
    repaint.add_argument("--lyrics", help="lyrics for this repaint only; defaults to the parent version")
    repaint.add_argument("--strength", choices=list(REPAINT_STRENGTHS))
    repaint.add_argument(
        "--instruction",
        help="override ACE's DiT task template; steering belongs in --style",
    )
    repaint.add_argument("--feedback-json", type=_parse_feedback)
    repaint.add_argument("--candidate-id")
    repaint.add_argument("--model", help="DiT for this repaint; defaults to the parent version's model")
    repaint.add_argument("--base-url", default=ACE_BASE_URL)
    repaint.add_argument("--poll-seconds", type=float, default=1.0)
    repaint.add_argument("--timeout-seconds", type=float, default=1800.0)
    repaint.add_argument("--quality", choices=["auto", "audio", "off"], default="auto",
                         help="check and finish the edited audio once; never automatically repaint again")

    cover = subparsers.add_parser("cover", help="create new versions based on a verified candidate audio")
    cover.add_argument("path", type=Path)
    cover.add_argument("--engine", choices=ENGINES, default=DEFAULT_ENGINE)
    cover.add_argument("--candidate-id", required=True)
    _add_seed_args(cover)
    cover.add_argument("--strength", type=float, default=0.7)
    cover.add_argument("--style")
    cover.add_argument("--lyrics")
    cover.add_argument("--model")
    cover.add_argument("--base-url", default=ACE_BASE_URL)
    cover.add_argument("--poll-seconds", type=float, default=1.0)
    cover.add_argument("--timeout-seconds", type=float, default=1800.0)
    cover.add_argument("--quality", choices=["auto", "audio", "off"], default="auto")
    cover.add_argument("--feedback-json", type=_parse_feedback)

    plan = subparsers.add_parser("plan", help="turn listening feedback into a proposed change")
    plan.add_argument("path", type=Path)
    plan.add_argument("--engine", choices=ENGINES, default=DEFAULT_ENGINE)
    plan.add_argument("candidate_id")
    plan.add_argument("--feedback", required=True)
    plan.add_argument("--start", type=float)
    plan.add_argument("--end", type=float)
    plan.add_argument("--strength", choices=assistant.STRENGTH_NAMES, default="medium")
    plan.add_argument("--versions", type=int, default=1)
    _add_assistant_args(plan)

    draft = subparsers.add_parser("draft", help="draft caption, lyrics and title from a description")
    draft.add_argument("--query", required=True)
    draft.add_argument("--engine", choices=ENGINES,
                       help="ask this engine's planner (ACE only) when no assistant writes the draft; "
                            "without it local rule templates write it and no music server is contacted")
    draft.add_argument("--instrumental", action="store_true")
    draft.add_argument("--duration", type=float, default=120.0)
    draft.add_argument("--base-url", default=ACE_BASE_URL)
    draft.add_argument("--production-rules-json", type=_parse_production_rules)
    draft.add_argument("--vocal-language", choices=["ko", "en"], default="ko")
    _add_assistant_args(draft)

    helper = subparsers.add_parser("assistant", help="inspect the feedback assistant")
    helper.add_argument("action", choices=["rules", "models"])
    _add_assistant_args(helper)

    export = subparsers.add_parser("export", help="export the selected candidate as WAV")
    export.add_argument("path", type=Path)
    export.add_argument("--output", type=Path)
    export.add_argument("--candidate-id", help="export this explicit version without changing the human final selection")
    export.add_argument("--unmastered", action="store_true",
                        help="export the version as the engine made it instead of its mastered listening copy")

    master = subparsers.add_parser("master", help="make the missing mastered listening copies of a song's versions")
    master.add_argument("path", type=Path)
    master.add_argument("--candidate-id", help="only this version")

    inspect = subparsers.add_parser("inspect", help="summarize and verify a reopened project")
    inspect.add_argument("path", type=Path)

    rhythm = subparsers.add_parser("analyze-rhythm", help="measure WAV rhythm, frame levels and spectrum without changing audio")
    rhythm.add_argument("path", type=Path)
    rhythm.add_argument("--bpm", type=float, help="compare the measured pulse with this requested quarter-note BPM and measure the tempo played near it")
    rhythm.add_argument("--time-signature", help="project the requested meter (2/4, 3/4, 4/4 or 6/8), without verifying downbeats")
    rhythm.add_argument("--style", default="", help="musical intent, such as rubato or tempo changes")
    rhythm.add_argument("--diagnostics", action="store_true", help="also analyze percussive timing and separately attributed backing continuity; loads no model")
    rhythm.add_argument("--separation-json", type=Path, help="explicit source-bound separated-energy JSON or audited legacy stem cache")
    rhythm.add_argument("--stem-label", help="label in a legacy stem cache")
    rhythm.add_argument("--evidence-audio", type=Path, help="explicit source audio measured by the cache; transfer only identical PCM windows")
    rhythm.add_argument("--intent-json", type=Path, help="explicit source-bound expected rhythm sections; implies diagnostics and loads no model")

    diagnose = subparsers.add_parser("inspect-rhythm", help="append a model-free rhythm diagnosis report to a saved candidate")
    diagnose.add_argument("path", type=Path)
    diagnose.add_argument("--candidate-id", required=True)
    diagnose.add_argument("--separation-json", type=Path)
    diagnose.add_argument("--stem-label")
    diagnose.add_argument("--evidence-candidate-id", help="verified source candidate originally measured by the explicit stem cache")
    diagnose.add_argument("--intent-json", type=Path, help="explicit source-bound expected rhythm sections")

    serve = subparsers.add_parser("serve", help="run the job service (arkAgent engine contract) on 127.0.0.1 until stopped")
    serve.add_argument("--port", type=int, default=47833)
    serve.add_argument("--output-dir", type=Path, help="folder for service job records (jobs/); default: output/ in this checkout")

    return parser


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    if args.engine == MUSIC3_ENGINE and (args.start is not None or args.end is not None):
        raise ValueError("Music 3 does not support range repaint; request a whole-song regeneration")
    store = ProjectStore(args.path)
    project = store.load()
    rows = {row["candidateId"]: row for row in candidate_rows(store, project)}
    candidate = rows.get(args.candidate_id)
    if candidate is None:
        raise KeyError(f"candidate not found: {args.candidate_id}")
    if (args.start is None) != (args.end is None):
        raise ValueError("pass both --start and --end, or neither")
    edit_range = (
        {"startSeconds": args.start, "endSeconds": args.end} if args.start is not None else None
    )
    inputs = project["inputs"]
    caption = candidate.get("baseStylePrompt", candidate.get("stylePrompt", inputs["stylePrompt"]))
    if not caption:
        preset = (candidate.get("productionRules") or {}).get("preset")
        caption = preset["caption"] if preset else candidate.get("stylePrompt", "")
    request = assistant.PlanRequest(
        feedback=args.feedback,
        caption=caption,
        lyrics=candidate["lyrics"],
        duration_seconds=float(candidate.get("durationSeconds") or inputs["targetDurationSeconds"]),
        edit_range=edit_range,
        strength=args.strength,
        versions=args.versions,
        bpm=candidate.get("bpm"),
        key_scale=candidate.get("keyScale"),
        engine=args.engine,
    )
    plan = assistant.plan_revision(request, _llm_config(args))
    if args.engine == MUSIC3_ENGINE:
        plan.update(action="regenerate", range=None)
        plan["summary"] = f"의견을 반영해 새 곡 {args.versions}개를 만들어요."
        plan.setdefault("notes", []).append("Music 3에서는 가사와 스타일을 바탕으로 곡 전체를 새로 만들어요.")
    plan["baseStylePrompt"] = candidate.get("baseStylePrompt", request.caption)
    plan["candidateId"] = args.candidate_id
    return plan


def _source_models(path: Path, candidate_id: str | None) -> tuple[str, str]:
    """DiT and LM a version was made with; ACE starts with them for edits of that version."""

    try:
        project = ProjectStore(path).load()
        candidate = ProjectStore.find_by_id(project, "candidates", "candidateId",
                                            candidate_id or project.get("selectedCandidateId"))
        original = ProjectStore.find_by_id(project, "requests", "requestId", candidate["requestId"])["parameters"]
        return original.get("model", ACE_DIT_MODEL), original.get("lm_model_path", DEFAULT_LM_MODEL)
    except (KeyError, ValueError, TypeError, OSError):
        # The run itself reports what is wrong with the project or version.
        return ACE_DIT_MODEL, DEFAULT_LM_MODEL


def _batch_models(path: Path, job_id: str | None) -> tuple[str, str]:
    """DiT and LM a batch froze; resuming it starts ACE with them."""

    try:
        project = ProjectStore(path).load()
        payload = frozen_batch_payloads(project, resume_source(project, job_id))[0]
        return payload.get("model", ACE_DIT_MODEL), payload.get("lm_model_path", DEFAULT_LM_MODEL)
    except (KeyError, ValueError, TypeError, IndexError, OSError):
        return ACE_DIT_MODEL, DEFAULT_LM_MODEL


def run(args: argparse.Namespace) -> Any:
    if args.command == "inspect-rhythm":
        from .rhythm_inspection import inspect_saved_candidate
        return inspect_saved_candidate(args.path, candidate_id=args.candidate_id, cache_path=args.separation_json,
            stem_label=args.stem_label, evidence_candidate_id=args.evidence_candidate_id, intent_path=args.intent_json)
    if args.command == "analyze-rhythm":
        if args.diagnostics or args.separation_json is not None or args.intent_json is not None:
            from .rhythm_inspection import inspect_audio_rhythm
            from .rhythm_evidence import load_cached_stem_evidence
            from .rhythm_intent import load_rhythm_intent
            intent = load_rhythm_intent(args.intent_json, audio=args.path) if args.intent_json is not None else None
            stems = load_cached_stem_evidence(args.separation_json, audio=args.path, label=args.stem_label,
                evidence_audio=args.evidence_audio) if args.separation_json is not None else None
            if args.separation_json is None and (args.stem_label is not None or args.evidence_audio is not None):
                raise ValueError("Stem label/source audio requires explicit separated-energy JSON")
            return inspect_audio_rhythm(args.path, requested_bpm=args.bpm, time_signature=args.time_signature,
                style_prompt=args.style, stem_inputs=stems, intent_snapshot=intent)
        if args.stem_label is not None or args.evidence_audio is not None:
            raise ValueError("Stem label/source audio requires explicit separated-energy JSON")
        from .music_structure import analyze_music_structure
        from .played_tempo import song_tempo
        from .storage import sha256_file
        return {**analyze_music_structure(args.path, requested_bpm=args.bpm,
            time_signature=args.time_signature, style_prompt=args.style),
            "playedTempo": song_tempo(args.path, reference_bpm=args.bpm, reference_source="request" if args.bpm else "none"),
            "measuredArtifactSha256": sha256_file(args.path)}
    if args.command == "song-plan":
        controls = args.input_json
        return prepare_song_plan(controls["lyrics"], duration_seconds=controls["durationSeconds"],
                                 bpm=controls["bpm"], time_signature=controls["timeSignature"],
                                 preset_id=controls["presetId"], instrumental=controls["instrumental"],
                                 development=controls["development"], breathing=controls["breathing"])
    if args.command == "production-rules":
        return production_catalog(engine=args.engine)
    if args.command == "capabilities":
        return {"engine": ACE_ENGINE, "model": ACE_DIT_MODEL, "capabilities": dict(ACE_CAPABILITIES),
                "maxDurationSeconds": MAX_DURATION_SECONDS}
    if args.command == "init":
        lyrics = _text_arg(args.lyrics, args.lyrics_file)
        if not math.isfinite(args.duration) or args.duration < 10 or args.duration > MAX_DURATION_SECONDS:
            raise ValueError("duration must be between 10 and 300 seconds")
        store = ProjectStore.initialize(
            args.path,
            title=args.title,
            lyrics=lyrics,
            style_prompt=args.style,
            target_duration_seconds=args.duration,
            structure=args.structure,
            bpm=args.bpm,
            key_scale=args.key,
            time_signature=args.time_signature,
            production_rules=args.production_rules_json,
        )
        project = store.load()
        return {"projectId": project["projectId"], "path": str(store.root)}
    if args.command == "generate":
        return generate_candidates(
            args.path,
            seeds=_seeds(args),
            base_url=args.base_url,
            model=args.model,
            lm_model=args.lm_model,
            lm_temperature=args.lm_temperature,
            style_prompt=args.style,
            lyrics=args.lyrics,
            bpm=args.bpm,
            feedback=args.feedback_json,
            source_candidate_id=args.source_candidate_id,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
            quality=quality_policy(args.quality, max_attempts=args.quality_attempts, repair_attempts=args.repair_attempts),
            engine=args.engine,
            client_factory=managed_client_factory(model=args.model or ACE_DIT_MODEL, lm_model=args.lm_model)
            if args.engine == ACE_ENGINE else None,
        )
    if args.command == "cover":
        model, lm_model = _source_models(args.path, args.candidate_id)
        return cover_candidates(args.path, candidate_id=args.candidate_id, seeds=_seeds(args),
                                strength=args.strength, style_prompt=args.style, lyrics=args.lyrics,
                                base_url=args.base_url, model=args.model, feedback=args.feedback_json,
                                poll_seconds=args.poll_seconds, timeout_seconds=args.timeout_seconds,
                                quality=quality_policy(args.quality, max_attempts=1), engine=args.engine,
                                client_factory=managed_client_factory(model=args.model or model, lm_model=lm_model))
    if args.command == "resume":
        model, lm_model = _batch_models(args.path, args.job_id)
        return resume_latest_batch(
            args.path,
            job_id=args.job_id,
            base_url=args.base_url,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
            engine=args.engine,
            client_factory=managed_client_factory(model=model, lm_model=lm_model) if args.engine == ACE_ENGINE else None,
        )
    if args.command == "recover":
        return recover_project(args.path)
    if args.command == "candidates":
        store = ProjectStore(args.path)
        project = store.load()
        return {
            "projectId": project["projectId"],
            "title": project["title"],
            "selectedCandidateId": project.get("selectedCandidateId"),
            "candidates": candidate_rows(store, project),
        }
    if args.command == "status":
        store = ProjectStore(args.path)
        return project_status(store, store.load())
    if args.command == "library":
        return {"songs": library(args.dir, args.project)}
    if args.command == "select":
        return select_candidate(args.path, args.candidate_id)
    if args.command == "undo-selection":
        return undo_selection(args.path)
    if args.command == "review":
        return review_candidate(
            args.path,
            args.candidate_id,
            status=args.status,
            note=args.note,
            rating=args.rating,
        )
    if args.command == "revise":
        revision = revise_inputs(
            args.path,
            title=args.title,
            style_prompt=args.style,
            lyrics=_text_arg(args.lyrics, args.lyrics_file),
            duration_seconds=args.duration,
            bpm=args.bpm,
            key_scale=args.key,
            time_signature=args.time_signature,
            production_rules=args.production_rules_json,
            reason="manual",
        )
        return {"changed": revision is not None, "revision": revision}
    if args.command == "repaint":
        model, lm_model = _source_models(args.path, args.candidate_id)
        return repaint_candidate(
            args.path,
            start_seconds=args.start,
            end_seconds=args.end,
            seed=args.seed,
            instruction=args.instruction,
            style_prompt=args.style,
            lyrics=args.lyrics,
            strength=args.strength,
            feedback=args.feedback_json,
            candidate_id=args.candidate_id,
            base_url=args.base_url,
            model=args.model,
            engine=args.engine,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
            quality=quality_policy(args.quality, max_attempts=1),
            client_factory=managed_client_factory(model=args.model or model, lm_model=lm_model),
        )
    if args.command == "plan":
        return _plan(args)
    if args.command == "draft":
        def draft(**engine_client: Any) -> Any:
            return draft_song(
                args.query,
                instrumental=args.instrumental,
                duration_seconds=args.duration,
                base_url=args.base_url,
                llm=_llm_config(args),
                production_rules=args.production_rules_json,
                vocal_language=args.vocal_language, engine=args.engine, **engine_client,
            )
        if args.engine != ACE_ENGINE:
            return draft()
        # Asking ACE's planner is engine work: it holds the lane and brings ACE up only for this draft.
        with shared_lane():
            return draft(client_factory=managed_client_factory(model=ACE_DIT_MODEL, lm_model=DEFAULT_LM_MODEL))
    if args.command == "assistant":
        if args.action == "rules":
            return {"rules": assistant.catalog()}
        config = _llm_config(args)
        if config is None:
            return {"models": []}
        return {"models": assistant.list_models(config)}
    if args.command == "export":
        return export_selected(args.path, output=args.output, candidate_id=args.candidate_id, unmastered=args.unmastered)
    if args.command == "master":
        return master_versions(args.path, candidate_id=args.candidate_id)
    if args.command == "inspect":
        store = ProjectStore(args.path)
        project = store.load()
        invalid, retired = [], []
        for artifact in project["artifacts"]:
            if artifact.get("retired"):
                # A replaced listening copy's file is removed on purpose; its record stays as history.
                retired.append({"artifactId": artifact["artifactId"], "supersededBy": artifact["retired"].get("supersededBy")})
                continue
            valid, reason = store.verify_artifact(artifact)
            if not valid:
                invalid.append({"artifactId": artifact["artifactId"], "reason": reason})
        return {
            "schemaVersion": project["schemaVersion"],
            "projectId": project["projectId"],
            "title": project["title"],
            "selectedCandidateId": project.get("selectedCandidateId"),
            "counts": {
                "requests": len(project["requests"]),
                "candidates": len(project["candidates"]),
                "artifacts": len(project["artifacts"]),
                "findings": len(project["findings"]),
                "jobs": len(project["jobs"]),
                "revisions": len(project["revisions"]),
                "feedback": len(project.get("feedback", [])),
            },
            "invalidArtifacts": invalid,
            "retiredArtifacts": retired,
            "restorable": not invalid,
        }
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            # Long-running: prints one JSON document once it listens, then serves until SIGINT/SIGTERM.
            from .service.server import serve
            raise SystemExit(serve(port=args.port, output_dir=args.output_dir))
        _print(run(args))
    except KeyboardInterrupt:
        print("cancelled by user", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
