"""Command-line interface for project creation, generation, review, and export."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .storage import ProjectStore
from .workflow import (
    DEFAULT_BASE_URL,
    export_selected,
    generate_candidates,
    repaint_candidate,
    resume_latest_batch,
    review_candidate,
    select_candidate,
    undo_selection,
)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music-engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a project")
    init.add_argument("path", type=Path)
    init.add_argument("--title", required=True)
    lyrics_group = init.add_mutually_exclusive_group(required=True)
    lyrics_group.add_argument("--lyrics")
    lyrics_group.add_argument("--lyrics-file", type=Path)
    init.add_argument("--style", required=True)
    init.add_argument("--duration", type=float, required=True)
    init.add_argument("--structure")

    generate = subparsers.add_parser("generate", help="start a new candidate batch")
    generate.add_argument("path", type=Path)
    generate.add_argument("--seeds", type=_parse_seeds, required=True)
    generate.add_argument("--base-url", default=DEFAULT_BASE_URL)
    generate.add_argument("--model", default="acestep-v15-turbo")
    generate.add_argument("--lm-model", default="acestep-5Hz-lm-0.6B")
    generate.add_argument("--poll-seconds", type=float, default=1.0)
    generate.add_argument("--timeout-seconds", type=float, default=1800.0)

    resume = subparsers.add_parser("resume", help="explicitly resume the latest candidate batch")
    resume.add_argument("path", type=Path)
    resume.add_argument("--base-url")
    resume.add_argument("--poll-seconds", type=float, default=1.0)
    resume.add_argument("--timeout-seconds", type=float, default=1800.0)

    candidates = subparsers.add_parser("candidates", help="list candidates and reviews")
    candidates.add_argument("path", type=Path)

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

    repaint = subparsers.add_parser("repaint", help="create a non-destructive repaint candidate")
    repaint.add_argument("path", type=Path)
    repaint.add_argument("--start", type=float, required=True)
    repaint.add_argument("--end", type=float, required=True)
    repaint.add_argument("--instruction", required=True)
    repaint.add_argument("--seed", type=int, required=True)
    repaint.add_argument("--candidate-id")
    repaint.add_argument("--base-url", default=DEFAULT_BASE_URL)
    repaint.add_argument("--poll-seconds", type=float, default=1.0)
    repaint.add_argument("--timeout-seconds", type=float, default=1800.0)

    export = subparsers.add_parser("export", help="export the selected candidate as WAV")
    export.add_argument("path", type=Path)
    export.add_argument("--output", type=Path)

    inspect = subparsers.add_parser("inspect", help="summarize and verify a reopened project")
    inspect.add_argument("path", type=Path)

    return parser


def _candidate_rows(store: ProjectStore, project: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = {record["artifactId"]: record for record in project["artifacts"]}
    requests = {record["requestId"]: record for record in project["requests"]}
    findings = {record["findingId"]: record for record in project["findings"]}
    rows = []
    for candidate in project["candidates"]:
        artifact = artifacts[candidate["artifactId"]]
        valid, reason = store.verify_artifact(artifact)
        request = requests[candidate["requestId"]]
        rows.append(
            {
                "candidateId": candidate["candidateId"],
                "selected": candidate["candidateId"] == project.get("selectedCandidateId"),
                "seed": request["parameters"].get("seed"),
                "taskType": request["parameters"].get("task_type"),
                "parentCandidateId": candidate.get("parentCandidateId"),
                "editRange": candidate.get("editRange"),
                "durationSeconds": artifact.get("audio", {}).get("durationSeconds"),
                "artifactValid": valid,
                "artifactValidation": reason,
                "humanReview": candidate["humanReview"],
                "findings": [
                    findings[finding_id]
                    for finding_id in candidate["findingIds"]
                    if finding_id in findings
                ],
                "path": str(store.resolve_artifact(artifact)),
            }
        )
    return rows


def run(args: argparse.Namespace) -> Any:
    if args.command == "init":
        lyrics = (
            args.lyrics_file.read_text(encoding="utf-8")
            if args.lyrics_file is not None
            else args.lyrics
        )
        if args.duration < 10 or args.duration > 600:
            raise ValueError("duration must be between 10 and 600 seconds")
        store = ProjectStore.initialize(
            args.path,
            title=args.title,
            lyrics=lyrics,
            style_prompt=args.style,
            target_duration_seconds=args.duration,
            structure=args.structure,
        )
        project = store.load()
        return {"projectId": project["projectId"], "path": str(store.root)}
    if args.command == "generate":
        return generate_candidates(
            args.path,
            seeds=args.seeds,
            base_url=args.base_url,
            model=args.model,
            lm_model=args.lm_model,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    if args.command == "resume":
        return resume_latest_batch(
            args.path,
            base_url=args.base_url,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    if args.command == "candidates":
        store = ProjectStore(args.path)
        project = store.load()
        return {
            "projectId": project["projectId"],
            "selectedCandidateId": project.get("selectedCandidateId"),
            "candidates": _candidate_rows(store, project),
        }
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
    if args.command == "repaint":
        return repaint_candidate(
            args.path,
            start_seconds=args.start,
            end_seconds=args.end,
            instruction=args.instruction,
            seed=args.seed,
            candidate_id=args.candidate_id,
            base_url=args.base_url,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    if args.command == "export":
        return export_selected(args.path, output=args.output)
    if args.command == "inspect":
        store = ProjectStore(args.path)
        project = store.load()
        invalid = []
        for artifact in project["artifacts"]:
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
            },
            "invalidArtifacts": invalid,
            "restorable": not invalid,
        }
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _print(run(args))
    except KeyboardInterrupt:
        print("cancelled by user", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
