"""Opt-in real Music 3 generation and integrity/quality checks.

Start scripts/start_music3_api.sh separately. This runner creates fresh projects,
never downloads models, never imports a mock client, and never records a human
listening verdict. Failed or uncertain quality observations remain in the report.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from local_music_engine.auto_quality import quality_policy
from local_music_engine.music3_adapter import Music3Client
from local_music_engine.production_rules import catalog
from local_music_engine.storage import ProjectStore, atomic_write_json, utc_now
from local_music_engine.workflow import export_selected, generate_candidates, resume_latest_batch


EN = """[Verse]
City lights are fading
Your voice stays with me
I walk through the silence
And let the night breathe

[Chorus]
Stay until the morning
Let the cold wind go
Step into the daylight
We can take it slow
"""
EN_LONG = EN + """
[Verse]
I leave the hurt behind
With every step we take
Your hand is warm in mine
The sky begins to change

[Chorus]
Stay until the morning
Let the cold wind go
Step into the daylight
We can take it slow
"""
KO = """[Verse]
불빛이 멀어져
네 목소리 남아
조용히 걸으며
긴 밤을 보내네

[Chorus]
아침이 올 때면
찬 바람 놓아줘
빛으로 걸어가
천천히 가도 돼
"""
CASES = {
    "en30": ("en", 30, EN, 202610501),
    "ko30": ("ko", 30, KO, 202610502),
    "en60": ("en", 60, EN_LONG, 202610503),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--projects-dir", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18002")
    parser.add_argument("--cases", default="en30,ko30,en60")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    names = args.cases.split(",")
    if not names or len(names) != len(set(names)) or any(name not in CASES for name in names):
        parser.error("choose distinct en30,ko30,en60 cases")
    output = args.output_dir.expanduser().absolute()
    if output.exists():
        raise FileExistsError("smoke output already exists; use a fresh directory")
    output.mkdir(parents=True)
    project_base = args.projects_dir.expanduser().absolute() if args.projects_dir else output / "projects"
    project_base.mkdir(parents=True, exist_ok=True)
    health = Music3Client(args.base_url).health()
    if health.get("engine") != "minimax-music3" or not health.get("models_initialized"):
        raise RuntimeError("actual Music 3 server is not ready")
    if health.get("realInference") is not True:
        raise RuntimeError("runtime has not declared a real weight-backed inference backend")
    preset = next(item for item in catalog()["presets"] if item["id"] == "emotional-hiphop")
    selection = {"version": 1, "presetId": preset["id"], "ruleIds": [*preset["ruleIds"], "section-development", "phrase-breathing"]}
    stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S")
    report_path = output / "report.json"
    report = {"status": "running", "createdAt": utc_now(), "realInference": True,
              "humanReview": "unreviewed", "engineBefore": health, "cases": [],
              "comparisonScope": "real runtime/quality observations; no claim that Music 3 beats ACE without matched listening"}
    atomic_write_json(report_path, report)
    failed = False
    for name in names:
        language, duration, lyrics, seed = CASES[name]
        root = project_base / f"music3-emotional-{name}-{stamp}"
        record = {"case": name, "language": language, "requestedDurationSeconds": duration,
                  "projectPath": str(root), "seed": seed, "status": "generating"}
        report["cases"].append(record)
        atomic_write_json(report_path, report)
        try:
            store = ProjectStore.initialize(root, title=f"City Lights · Music 3 {language.upper()} {duration}s",
                lyrics=lyrics, style_prompt="intimate male melodic rap, mellow late-night mood, expressive clear lead vocal",
                target_duration_seconds=duration, bpm=84, key_scale="A minor", time_signature="4",
                production_rules=selection)
            started = time.monotonic()
            result = generate_candidates(root, seeds=[seed], base_url=args.base_url,
                engine="minimax-music3", quality=quality_policy("auto", max_attempts=1), timeout_seconds=args.timeout)
            record["totalSeconds"] = round(time.monotonic() - started, 3)
            project = store.load()
            jobs = [job for job in project["jobs"] if job["kind"] == "generate-candidate"]
            record.update(generation=result, submittedAttempts=sum(bool(job.get("remoteTaskId")) for job in jobs),
                successfulInferenceJobs=sum(job["status"] == "succeeded" for job in jobs))
            assert len(jobs) <= 1, "the real probe must not perform hidden retries"
            if not result.get("recommendedCandidateId"):
                raise RuntimeError(f"generation returned no candidate: {result.get('failures', result)}")
            candidate = store.find_by_id(project, "candidates", "candidateId", result["recommendedCandidateId"])
            artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
            assert candidate["quality"]["complete"]
            assert all(store.verify_artifact(item)[0] for item in project["artifacts"])
            assert all(item["humanReview"]["status"] == "unreviewed" for item in project["candidates"])
            assert project["selectedCandidateId"] is None
            export = export_selected(root, candidate_id=candidate["candidateId"], output=output / f"{name}.wav")
            assert hashlib.sha256(Path(export["externalPath"]).read_bytes()).hexdigest() == artifact["sha256"]
            record.update(status="generated", generation=result, quality=candidate["quality"],
                export=export, audio=artifact["audio"], generationAttempts=1, allArtifactsVerified=True)
            atomic_write_json(report_path, report)
            resumed = resume_latest_batch(root, base_url=args.base_url, engine="minimax-music3")
            assert not resumed["newCandidateIds"]
            assert len([job for job in store.load()["jobs"] if job["kind"] == "generate-candidate"]) == 1
            assert store.load()["inputs"]["lyricsOriginal"] == lyrics
            record.update(status="completed", explicitResume=resumed, humanReview="unreviewed")
        except Exception as error:
            record.update(status="failed", error=f"{type(error).__name__}: {error}")
            failed = True
        finally:
            record["finishedAt"] = utc_now()
            atomic_write_json(report_path, report)
    try:
        after_health = Music3Client(args.base_url).health()
    except Exception as error:
        after_health = {"available": False, "error": f"{type(error).__name__}: {error}"}
        failed = True
    report.update(status="partial" if failed else "completed", finishedAt=utc_now(), engineAfter=after_health,
        actualSuccessfulGenerationCount=sum(item.get("successfulInferenceJobs", 0) for item in report["cases"]))
    atomic_write_json(report_path, report)
    print(json.dumps({"reportPath": str(report_path), "status": report["status"],
        "cases": [{"case": row["case"], "status": row["status"], "output": row.get("export", {}).get("externalPath"),
                   "quality": row.get("quality", {}).get("status"), "seconds": row.get("totalSeconds")} for row in report["cases"]]},
        ensure_ascii=False, indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
