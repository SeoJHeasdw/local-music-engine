#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ACE_ROOT="$PROJECT_ROOT/.runtime/ace-step-1.5"
MODEL_ROOT="$PROJECT_ROOT/.runtime/models"
PORT="${MUSIC_ENGINE_ACE_PORT:-18001}"
# The server loads one DiT and one LM at start and ignores per-request model names
# for anything else, so the model choice belongs here, not in each request.
DIT_MODEL="${MUSIC_ENGINE_ACE_DIT_MODEL:-acestep-v15-turbo}"
LM_MODEL="${MUSIC_ENGINE_ACE_LM_MODEL:-acestep-5Hz-lm-4B}"

if [[ ! -x "$ACE_ROOT/.venv/bin/acestep-api" ]]; then
  echo "ACE runtime is missing. Run ./scripts/bootstrap_ace.sh first." >&2
  exit 1
fi

export ACESTEP_CHECKPOINTS_DIR="$MODEL_ROOT"
export ACESTEP_LM_BACKEND="mlx"
export ACESTEP_CONFIG_PATH="$DIT_MODEL"
export ACESTEP_LM_MODEL_PATH="$LM_MODEL"
export ACESTEP_INIT_LLM="true"
export ACESTEP_NO_INIT="false"
export ACESTEP_DOWNLOAD_SOURCE="huggingface"
export TOKENIZERS_PARALLELISM="false"
export HF_HUB_DISABLE_TELEMETRY="1"

cd "$ACE_ROOT"
# ace_api_server.py wraps ACE's own server CLI; see its docstring for the memory fix.
exec .venv/bin/python "$SCRIPT_DIR/ace_api_server.py" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --download-source huggingface \
  --init-llm \
  --lm-model-path "$LM_MODEL"
