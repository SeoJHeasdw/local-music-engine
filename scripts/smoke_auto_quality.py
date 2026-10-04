"""Opt-in real generation + local quality checks; start ACE separately first.

uv run python scripts/smoke_auto_quality.py --output-dir .runtime/auto-smoke-001
Creates a fresh project only. No fake client, human approval or external audio API.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from local_music_engine.ace_adapter import AceStepClient
from local_music_engine.auto_quality import quality_policy
from local_music_engine.storage import ProjectStore, atomic_write_json
from local_music_engine.workflow import export_selected, generate_candidates, resume_latest_batch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--duration", type=int, default=30, choices=range(10, 601))
    parser.add_argument("--seed", type=int, default=64031)
    args = parser.parse_args()
    health = AceStepClient(args.base_url).health()
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = output / "song"
    store = ProjectStore.initialize(root, title="별빛 산책 · 자동 품질 실측",
        lyrics="[Verse]\n창가에 내려앉은 작은 별빛\n조용히 오늘의 길을 비추네\n[Chorus]\n천천히 걸어가 우리 함께\n새로운 아침이 올 때까지",
        style_prompt="Korean acoustic pop, warm female vocal, gentle piano, soft drums, clear Korean diction",
        target_duration_seconds=args.duration)
    start = time.monotonic()
    result = generate_candidates(root, seeds=[args.seed], base_url=args.base_url, quality=quality_policy())
    elapsed = round(time.monotonic() - start, 3)
    project = store.load()
    generation_jobs = [job for job in project["jobs"] if job["kind"] == "generate-candidate"]
    assert 1 <= len(generation_jobs) <= 4, result
    assert result["candidateIds"], result
    recommended = result["recommendedCandidateId"]
    winner = store.find_by_id(project, "candidates", "candidateId", recommended)
    assert winner["quality"]["complete"]
    assert winner["parentCandidateId"] and winner["quality"].get("processing")
    assert all(store.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    assert all(candidate["humanReview"]["status"] == "unreviewed" for candidate in project["candidates"])
    assert project["selectedCandidateId"] is None
    exported = export_selected(root, candidate_id=recommended, output=output / "recommended.wav")
    start = time.monotonic()
    resumed = resume_latest_batch(root, base_url=args.base_url)
    reuse_elapsed = round(time.monotonic() - start, 3)
    assert resumed["newCandidateIds"] == [] and resumed["reusedCandidateIds"] == result["candidateIds"], resumed
    project = store.load()
    assert len([job for job in project["jobs"] if job["kind"] == "generate-candidate"]) == len(generation_jobs)
    report = {"engine": health, "realGeneration": True, "result": result, "quality": winner["quality"],
              "generationAttempts": len(generation_jobs), "totalSeconds": elapsed, "reuseSeconds": reuse_elapsed,
              "export": exported, "resume": resumed, "allArtifactsVerified": True,
              "humanListening": "unreviewed", "selectedCandidateId": project["selectedCandidateId"]}
    atomic_write_json(output / "report.json", report)
    print(json.dumps({"reportPath": str(output / "report.json"), "status": result["status"],
                      "generationAttempts": len(generation_jobs), "qualityStatus": winner["quality"]["status"],
                      "lyricStatus": winner["quality"]["lyrics"]["status"], "totalSeconds": elapsed,
                      "reuseSeconds": reuse_elapsed, "allArtifactsVerified": True,
                      "humanListening": "unreviewed"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
