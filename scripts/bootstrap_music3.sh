#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime/minimax-music3"
SOURCE_ROOT="$RUNTIME_ROOT/source"
MODEL_ROOT="$PROJECT_ROOT/.runtime/models/minimax-music3-bf16"
UPSTREAM_COMMIT="784b29e2691a93ca7483147d86f61859dfaa6296"
MODEL_REVISION="83a5f2d365673689df5c8f36e21e108751fd92ea"
PYTHON_VERSION="3.12.12"

command -v uv >/dev/null 2>&1 || { echo "uv is required." >&2; exit 1; }
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || {
  echo "The Music 3 MLX runtime requires Apple Silicon macOS." >&2; exit 1;
}
mkdir -p "$RUNTIME_ROOT" "$MODEL_ROOT"
if [[ ! -d "$SOURCE_ROOT/.git" ]]; then
  git clone https://github.com/Blaizzy/mlx-audio.git "$SOURCE_ROOT"
  git -C "$SOURCE_ROOT" checkout --detach "$UPSTREAM_COMMIT"
fi
if [[ "$(git -C "$SOURCE_ROOT" rev-parse HEAD)" != "$UPSTREAM_COMMIT" ]]; then
  echo "Music 3 upstream checkout must match $UPSTREAM_COMMIT." >&2; exit 1
fi
if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain)" ]]; then
  echo "Music 3 upstream checkout has local changes; refusing to overwrite them." >&2; exit 1
fi
uv python install "$PYTHON_VERSION"
if [[ ! -x "$RUNTIME_ROOT/.venv/bin/python" ]]; then
  uv venv --python "$PYTHON_VERSION" "$RUNTIME_ROOT/.venv"
fi
uv pip install --python "$RUNTIME_ROOT/.venv/bin/python" -r "$SCRIPT_DIR/requirements-music3.txt" "$SOURCE_ROOT"
export HF_HUB_DISABLE_TELEMETRY=1
"$RUNTIME_ROOT/.venv/bin/python" - "$MODEL_ROOT" "$MODEL_REVISION" "$SCRIPT_DIR" <<'PY'
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from huggingface_hub import snapshot_download

destination, revision = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, sys.argv[3])
import music3_api_server as server  # pinned shard sizes and SHA-256

assert revision == server.MODEL_REVISION
snapshot_download(
    repo_id=server.MODEL_ID,
    revision=revision,
    local_dir=destination,
    max_workers=6,
    allow_patterns=["*.json", "*.safetensors", "tokenizer/*", "scheduler/*", "LICENSE", "README.md"],
)
shards = {}
for name, (size, expected) in server.MODEL_SHARDS.items():
    path = destination / name
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if path.stat().st_size != size or digest != expected:
        raise SystemExit(f"{name} does not match the pinned SHA-256; delete it and run bootstrap again")
    shards[name] = {"bytes": size, "sha256": digest}
manifest = {"engine": "minimax-music3", "repository": server.MODEL_ID, "revision": revision,
            "upstreamCommit": server.UPSTREAM_COMMIT, "precision": "bf16", "shards": shards}
descriptor, name = tempfile.mkstemp(prefix=".runtime-manifest-", dir=destination)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(name, destination / "runtime-manifest.json")
PY
echo "Music 3 is ready at $RUNTIME_ROOT"
echo "Model files are stored at $MODEL_ROOT"
