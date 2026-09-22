"""Opt-in ACE integration check. Start the real server separately before running.

uv run python scripts/smoke_real_engine.py --output-dir .runtime/smoke-001

Creates a new project and real audio. Never imports a fake client, marks a candidate
listened/approved, downloads models, starts a server, or edits an existing project.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from local_music_engine.ace_adapter import AceStepClient
from local_music_engine.storage import ProjectStore, atomic_write_json
from local_music_engine.workflow import (
    export_selected,
    repaint_candidate,
    resume_latest_batch,
    review_candidate,
    revise_inputs,
    select_candidate,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--duration", type=int, default=30, choices=range(10, 601), metavar="10..600")
    args = parser.parse_args()
    base = AceStepClient(args.base_url).base_url
    health = AceStepClient(base).health()
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = output / "song"
    style = "Korean acoustic pop, warm female vocal, gentle piano, soft drums, clear Korean diction"
    lyrics = "[Verse]\n창가에 내려앉은 작은 별빛\n조용히 오늘의 길을 비추네\n[Chorus]\n천천히 걸어가 우리 함께\n새로운 아침이 올 때까지"
    store = ProjectStore.initialize(root, title="별빛 산책 · 실제 엔진 검사", lyrics=lyrics,
                                    style_prompt=style, target_duration_seconds=args.duration)
    start = time.monotonic()
    timings = {}
    # Capture to files rather than pipes: a noisy server error must not deadlock a
    # long-running validation while the parent polls the atomic manifest.
    with (output / "generate.json").open("w") as stdout, (output / "generate.stderr.log").open("w") as stderr:
        child = subprocess.Popen([
            sys.executable, "-m", "local_music_engine", "generate", str(root),
            "--seeds", "63011,63012", "--base-url", base,
        ], stdout=stdout, stderr=stderr)
        noted = False
        try:
            while child.poll() is None:
                project = store.load()
                if project["candidates"] and not noted and store.generation_active():
                    before = time.monotonic()
                    review_candidate(root, project["candidates"][0]["candidateId"], status="unreviewed",
                                     note="자동 저장 검증 메모. 사람 청취 평가 아님.")
                    revise_inputs(root, style_prompt="Korean jazz, brushed drums", lyrics="다음 버전의 가사",
                                  duration_seconds=60 if args.duration != 60 else 90)
                    timings["concurrentSaveSeconds"] = round(time.monotonic() - before, 4)
                    noted = True
                time.sleep(0.1)
        finally:
            if child.poll() is None:
                child.send_signal(2)  # SIGINT gives the CLI time to record cancellation.
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        if child.returncode:
            raise RuntimeError(f"generation failed; see {output / 'generate.stderr.log'}")
    timings["twoVersionsSeconds"] = round(time.monotonic() - start, 2)
    batch = json.loads((output / "generate.json").read_text())
    assert batch["status"] == "succeeded", batch
    assert noted, "concurrent persistence was not exercised"
    project = store.load()
    assert all(request["parameters"]["prompt"] == style for request in project["requests"])
    assert all(request["parameters"]["audio_duration"] == args.duration for request in project["requests"])
    assert project["candidates"][0]["humanReview"]["notes"]
    print(f"Generated two {args.duration}s versions; concurrent notes saved.", flush=True)

    start = time.monotonic()
    resumed = resume_latest_batch(root, base_url=base)
    timings["verifiedReuseSeconds"] = round(time.monotonic() - start, 3)
    assert resumed["newCandidateIds"] == []
    assert resumed["reusedCandidateIds"] == batch["candidateIds"]
    start = time.monotonic()
    edit = repaint_candidate(root, candidate_id=batch["candidateIds"][0], seed=63013,
                             start_seconds=min(8, args.duration / 5),
                             end_seconds=min(13, args.duration / 2), strength="light", base_url=base)
    timings["repaintSeconds"] = round(time.monotonic() - start, 2)
    project = store.load()
    assert project["requests"][-1]["parameters"]["lyrics"] == lyrics
    assert project["requests"][-1]["parameters"]["audio_duration"] == project["artifacts"][0]["audio"]["durationSeconds"]
    select_candidate(root, edit["candidateId"])
    exported = export_selected(root)
    project = store.load()
    assert all(store.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    assert all(candidate["humanReview"]["status"] == "unreviewed" for candidate in project["candidates"])
    report = {"engine": health, "timings": timings, "batch": batch, "resume": resumed,
              "repaint": edit, "export": exported, "allArtifactsVerified": True, "humanListening": "unreviewed"}
    atomic_write_json(output / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
