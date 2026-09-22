#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ACE_ROOT="$PROJECT_ROOT/.runtime/ace-step-1.5"
MODEL_ROOT="$PROJECT_ROOT/.runtime/models"
PORT="${MUSIC_ENGINE_ACE_PORT:-18001}"

if [[ ! -x "$ACE_ROOT/.venv/bin/acestep-api" ]]; then
  echo "ACE runtime is missing. Run ./scripts/bootstrap_ace.sh first." >&2
  exit 1
fi

export ACESTEP_CHECKPOINTS_DIR="$MODEL_ROOT"
export ACESTEP_LM_BACKEND="mlx"
export ACESTEP_CONFIG_PATH="acestep-v15-turbo"
export ACESTEP_LM_MODEL_PATH="acestep-5Hz-lm-0.6B"
export ACESTEP_INIT_LLM="true"
export ACESTEP_NO_INIT="false"
export ACESTEP_DOWNLOAD_SOURCE="huggingface"
export TOKENIZERS_PARALLELISM="false"
export HF_HUB_DISABLE_TELEMETRY="1"

cd "$ACE_ROOT"
exec .venv/bin/acestep-api \
  --host 127.0.0.1 \
  --port "$PORT" \
  --download-source huggingface \
  --init-llm \
  --lm-model-path acestep-5Hz-lm-0.6B
