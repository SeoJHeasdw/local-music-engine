"""ACE runs only on the pinned weights: the pin file, its checks and the server start."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ace_weights", ROOT / "scripts" / "ace_weights.py")
weights = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(weights)


def pin(path: Path, data: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"bytes": len(data), "sha256": weights._sha256(path)}


def test_every_pinned_revision_and_weight_hash_is_in_the_decisions_record() -> None:
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    repos = weights.load_pins()
    for repo in repos:
        assert repo["revision"] in decisions
        for relative, record in repo["files"].items():
            if relative.endswith(".safetensors"):
                assert record["sha256"] in decisions, relative
    start = (ROOT / "scripts" / "start_ace_api.sh").read_text(encoding="utf-8")
    assert {"acestep-v15-xl-turbo", "acestep-5Hz-lm-4B"} <= weights.pinned_models(repos)
    assert ":-acestep-v15-xl-turbo}" in start and ":-acestep-5Hz-lm-4B}" in start


def test_check_sees_missing_and_resized_files_and_verify_sees_changed_bytes(tmp_path: Path) -> None:
    models = tmp_path / "models"
    repos = [
        {"repo": "ACE-Step/Ace-Step1.5", "revision": "a" * 40, "localDir": ".",
         "files": {"vae/weights.safetensors": pin(models / "vae" / "weights.safetensors", b"vae")}},
        {"repo": "ACE-Step/acestep-5Hz-lm-4B", "revision": "b" * 40, "localDir": "acestep-5Hz-lm-4B",
         "files": {"model.safetensors": pin(models / "acestep-5Hz-lm-4B" / "model.safetensors", b"lm")}},
    ]
    assert weights.pinned_models(repos) == {"vae", "acestep-5Hz-lm-4B"}
    assert weights.problems(models, repos, hashes=False) == []
    assert weights.problems(models, repos, hashes=True) == []

    (models / "acestep-5Hz-lm-4B" / "model.safetensors").write_bytes(b"LM")  # same size, other bytes
    assert weights.problems(models, repos, hashes=False) == []
    assert weights.problems(models, repos, hashes=True) == [
        f"SHA-256 differs ACE-Step/acestep-5Hz-lm-4B@{'b' * 12}: model.safetensors"]

    (models / "vae" / "weights.safetensors").write_bytes(b"vae2")
    (models / "acestep-5Hz-lm-4B" / "model.safetensors").unlink()
    assert weights.problems(models, repos, hashes=False) == [
        f"size differs ACE-Step/Ace-Step1.5@{'a' * 12}: vae/weights.safetensors",
        f"missing ACE-Step/acestep-5Hz-lm-4B@{'b' * 12}: model.safetensors"]


def test_a_model_without_pins_is_refused_before_any_file_is_read(tmp_path: Path, capsys) -> None:
    assert weights.main(["check", "--models", str(tmp_path), "--model", "acestep-v15-xl-sft"]) == 1
    assert "not a pinned model: acestep-v15-xl-sft" in capsys.readouterr().err


def test_the_server_starts_offline_and_only_on_the_pinned_files(tmp_path: Path) -> None:
    """Run the real start script against stand-ins for ACE's runtime and server."""

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("start_ace_api.sh", "ace_weights.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (scripts / "ace_api_server.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'offline': os.environ.get('HF_HUB_OFFLINE'), 'args': sys.argv[1:]}))\n")
    venv = tmp_path / ".runtime" / "ace-step-1.5" / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    (venv / "acestep-api").write_text("#!/bin/sh\n")
    (venv / "acestep-api").chmod(0o755)
    models = tmp_path / ".runtime" / "models"
    repos = [{"repo": f"ACE-Step/{name}", "revision": "c" * 40, "localDir": name,
              "files": {"model.safetensors": pin(models / name / "model.safetensors", name.encode())}}
             for name in ("acestep-v15-xl-turbo", "acestep-5Hz-lm-4B")]
    (scripts / "ace-weights.json").write_text(json.dumps({"repos": repos}))
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MUSIC_ENGINE_ACE", "HF_"))}

    def start(**overrides: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(scripts / "start_ace_api.sh")], env={**environment, **overrides},
                              capture_output=True, text=True, timeout=30)

    started = start()
    assert started.returncode == 0, started.stderr
    assert json.loads(started.stdout)["offline"] == "1"

    refused = start(MUSIC_ENGINE_ACE_DIT_MODEL="acestep-v15-xl-sft")
    assert refused.returncode == 1 and refused.stdout == ""
    assert "not a pinned model: acestep-v15-xl-sft" in refused.stderr

    (models / "acestep-5Hz-lm-4B" / "model.safetensors").unlink()
    refused = start()
    assert refused.returncode == 1 and refused.stdout == ""
    assert "missing ACE-Step/acestep-5Hz-lm-4B" in refused.stderr
