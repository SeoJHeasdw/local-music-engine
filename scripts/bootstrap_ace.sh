#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime"
ACE_ROOT="$RUNTIME_ROOT/ace-step-1.5"
MODEL_ROOT="$RUNTIME_ROOT/models"
ACE_TAG="v0.1.8"
PYTHON_VERSION="3.12.12"

command -v uv >/dev/null 2>&1 || {
  echo "uv is required. Install it with: brew install uv" >&2
  exit 1
}

mkdir -p "$RUNTIME_ROOT" "$MODEL_ROOT"
if [[ ! -d "$ACE_ROOT/.git" ]]; then
  git clone --branch "$ACE_TAG" --depth 1 https://github.com/ACE-Step/ACE-Step-1.5.git "$ACE_ROOT"
fi

current_commit="$(git -C "$ACE_ROOT" rev-parse HEAD)"
expected_commit="dce621408bee8c31b4fcf4811682eb9359e1bc94"
if [[ "$current_commit" != "$expected_commit" ]]; then
  echo "ACE checkout does not match $ACE_TAG ($expected_commit): $current_commit" >&2
  exit 1
fi

uv python install "$PYTHON_VERSION"
(
  cd "$ACE_ROOT"
  UV_PROJECT_ENVIRONMENT=.venv uv sync --locked --python "$PYTHON_VERSION"
)

if [[ -d "$ACE_ROOT/checkpoints" && ! -L "$ACE_ROOT/checkpoints" ]]; then
  echo "Refusing to replace existing ACE checkpoints directory: $ACE_ROOT/checkpoints" >&2
  exit 1
fi
if [[ ! -e "$ACE_ROOT/checkpoints" ]]; then
  ln -s ../models "$ACE_ROOT/checkpoints"
fi

# Weights come only from the revisions in ace-weights.json, and every file is checked
# against its SHA-256. The server starts offline and refuses to run without them.
"$ACE_ROOT/.venv/bin/python" -I "$SCRIPT_DIR/ace_weights.py" download --models "$MODEL_ROOT"

echo "ACE-Step $ACE_TAG is ready at $ACE_ROOT with the pinned weights in $MODEL_ROOT"
