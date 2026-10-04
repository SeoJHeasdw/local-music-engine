#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime/minimax-music3"
SOURCE_ROOT="$RUNTIME_ROOT/source"
MODEL_ROOT="$PROJECT_ROOT/.runtime/models/minimax-music3-mxfp8"
UPSTREAM_COMMIT="784b29e2691a93ca7483147d86f61859dfaa6296"
MODEL_REVISION="d00a12c3c7f80eb66379dd02dd0f30ed0ce2d96e"
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
"$RUNTIME_ROOT/.venv/bin/python" - "$MODEL_ROOT" "$MODEL_REVISION" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path
from huggingface_hub import snapshot_download

destination = Path(sys.argv[1])
revision = sys.argv[2]
snapshot_download(
    repo_id="mlx-community/MiniMax-Music3-mxfp8",
    revision=revision,
    local_dir=destination,
    max_workers=3,
    allow_patterns=["*.json", "*.safetensors", "tokenizer/*", "scheduler/*", "LICENSE", "README.md"],
)
manifest = {"engine": "minimax-music3", "repository": "mlx-community/MiniMax-Music3-mxfp8", "revision": revision,
            "upstreamCommit": "784b29e2691a93ca7483147d86f61859dfaa6296", "quantization": "mxfp8"}
descriptor, name = tempfile.mkstemp(prefix=".runtime-manifest-", dir=destination)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(name, destination / "runtime-manifest.json")
PY
echo "Music 3 MXFP8 is ready at $RUNTIME_ROOT"
echo "Model files are stored at $MODEL_ROOT"
