"""Opt-in real ACE-Step XL generation with model-free structure QC.

Start scripts/start_ace_api.sh separately, then run:
uv run python scripts/smoke_rhythm_quality.py --output-dir .runtime/rhythm-smoke-001

Writes a new instrumental project only. Never approves listening, downloads models,
starts a server, reuses another generation, or changes existing projects.
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    args = parser.parse_args()
    client = AceStepClient(args.base_url)
    health = client.health()
    if health.get("loaded_model") != "acestep-v15-xl-turbo":
        raise ValueError("Start the default ACE-Step XL turbo server for this validation")
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    store = ProjectStore.initialize(output / "song", title="리듬 QC · 실제 생성 검사", lyrics="[Instrumental]",
        style_prompt="Instrumental electronic pop, steady 120 BPM four-on-the-floor drums, warm bass and piano, consistent groove, natural ending",
        target_duration_seconds=30, bpm=120, time_signature="4/4")
    start = time.monotonic()
    with (output / "generation.json").open("w") as stdout, (output / "generation.stderr.log").open("w") as stderr:
        result = subprocess.run([sys.executable, "-m", "local_music_engine", "generate", str(store.root),
            "--engine", "ace-step", "--model", "acestep-v15-xl-turbo", "--seeds", "202610071",
            "--base-url", client.base_url, "--quality", "audio", "--quality-attempts", "1"],
            stdout=stdout, stderr=stderr, timeout=900, check=False)
    if result.returncode:
        raise RuntimeError(f"Real generation failed; see {output / 'generation.stderr.log'}")
    batch = json.loads((output / "generation.json").read_text())
    assert batch["status"] == "succeeded", batch
    project = store.load()
    assert len(project["candidates"]) == 2  # Immutable raw and separately measured playback copy.
    observations = []
    for candidate in project["candidates"]:
        artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
        assert store.verify_artifact(artifact)[0]
        assert candidate["humanReview"]["status"] == "unreviewed"
        rhythm = candidate["quality"]["rhythm"]
        assert rhythm["measuredArtifactSha256"] == artifact["sha256"]
        assert rhythm["requestedBpm"] == 120
        assert rhythm["frameSeries"]["points"] and len(rhythm["frameSeries"]["points"]) <= 2400
        assert all(not finding["retryEligible"] for finding in rhythm["findings"])
        observations.append({"candidateId": candidate["candidateId"], "path": artifact["path"],
            "sha256": artifact["sha256"], "status": rhythm["status"], "estimatedBpm": rhythm["estimatedBpm"],
            "confidence": rhythm["confidence"], "findings": rhythm["findings"], "framePointCount": len(rhythm["frameSeries"]["points"])})
    assert project["selectedCandidateId"] is None
    report = {"realInference": True, "engine": health, "generationSeconds": round(time.monotonic() - start, 2),
              "batch": batch, "observations": observations, "allArtifactsVerified": True, "humanReview": "unreviewed"}
    atomic_write_json(output / "report.json", report)
    print(json.dumps({key: report[key] for key in ("realInference", "generationSeconds", "observations", "humanReview")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
