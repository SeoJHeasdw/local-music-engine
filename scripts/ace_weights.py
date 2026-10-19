"""Keep ACE on the pinned weights in ``scripts/ace-weights.json``.

ACE downloads any model it finds missing when its server starts, from the newest
revision and, if Hugging Face fails, from ModelScope. ``start_ace_api.sh`` therefore
runs ``check`` first and starts ACE offline; ``bootstrap_ace.sh`` runs ``download``
(only the pinned revisions, only when files are missing) and then ``verify``.

    check     every pinned file is present at its pinned size, and the requested
              models (``--model``) are pinned; fast, for every server start
    verify    the same plus each file's SHA-256 (reads about 38 GB)
    download  fetch missing pinned files at their pinned revision, then verify

``check`` and ``verify`` use only the standard library; ``download`` needs the ACE
runtime's ``huggingface_hub``. Code files (``*.py``) are not pinned here: at every start ACE
copies them from its own pinned checkout into the model folders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

PINS = Path(__file__).with_name("ace-weights.json")


def load_pins(path: Path = PINS) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["repos"]


def pinned_models(repos: list[dict[str, Any]]) -> set[str]:
    """Model folder names under the checkpoints directory, e.g. ``acestep-v15-xl-turbo``."""

    return {(path.split("/", 1)[0] if repo["localDir"] == "." else repo["localDir"])
            for repo in repos for path in repo["files"]}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def problems(models: Path, repos: list[dict[str, Any]], *, hashes: bool) -> list[str]:
    found = []
    for repo in repos:
        for relative, pin in repo["files"].items():
            path = models / repo["localDir"] / relative
            label = f"{repo['repo']}@{repo['revision'][:12]}: {relative}"
            if not path.is_file():
                found.append(f"missing {label}")
            elif path.stat().st_size != pin["bytes"]:
                found.append(f"size differs {label}")
            elif hashes:
                print(f"hashing {relative}", file=sys.stderr, flush=True)
                if _sha256(path) != pin["sha256"]:
                    found.append(f"SHA-256 differs {label}")
    return found


def download(models: Path, repos: list[dict[str, Any]]) -> None:
    os.environ.pop("HF_HUB_OFFLINE", None)  # Read at import time.
    from huggingface_hub import snapshot_download

    for repo in repos:
        missing = [relative for relative, pin in repo["files"].items()
                   if not (models / repo["localDir"] / relative).is_file()]
        if not missing:
            continue
        print(f"downloading {repo['repo']} at {repo['revision']}: {len(missing)} files", file=sys.stderr, flush=True)
        snapshot_download(repo_id=repo["repo"], revision=repo["revision"], local_dir=str(models / repo["localDir"]),
                          allow_patterns=missing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("check", "verify", "download"))
    parser.add_argument("--models", type=Path, required=True, help="ACE checkpoints directory (.runtime/models)")
    parser.add_argument("--model", action="append", default=[], help="a DiT or LM the server will load")
    args = parser.parse_args(argv)
    repos = load_pins()
    unpinned = sorted(set(args.model) - pinned_models(repos))
    if unpinned:
        print(f"not a pinned model: {', '.join(unpinned)}. Add its revision and files to {PINS.name} "
              "and docs/DECISIONS.md first.", file=sys.stderr)
        return 1
    if args.action == "download":
        download(args.models, repos)
    found = problems(args.models, repos, hashes=args.action != "check")
    for line in found:
        print(line, file=sys.stderr)
    if found:
        print("ACE weights do not match the pins. Run ./scripts/bootstrap_ace.sh; it downloads missing pinned "
              "files. Remove a file whose size or SHA-256 differs first.", file=sys.stderr)
        return 1
    if args.action != "check":
        print(f"ACE weights match {PINS.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
