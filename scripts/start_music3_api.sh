#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime/minimax-music3"
HOST="${MUSIC_ENGINE_MUSIC3_HOST:-127.0.0.1}"
PORT="${MUSIC_ENGINE_MUSIC3_PORT:-18002}"
MODEL="${MUSIC_ENGINE_MUSIC3_MODEL:-mlx-community/MiniMax-Music3-mxfp8}"
if [[ "$MODEL" != "mlx-community/MiniMax-Music3-mxfp8" ]]; then
  echo "Music 3 requires the pinned MXFP8 checkpoint." >&2; exit 1
fi
case "$HOST" in
  127.0.0.1|localhost|::1) ;;
  *) echo "Music 3 host must be a loopback address." >&2; exit 1 ;;
esac
if [[ ! -x "$RUNTIME_ROOT/.venv/bin/python" ]]; then
  echo "Music 3 runtime is missing. Run ./scripts/bootstrap_music3.sh first." >&2
  exit 1
fi
export TOKENIZERS_PARALLELISM=false
export HF_HUB_DISABLE_TELEMETRY=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export MUSIC_ENGINE_MUSIC3_API_KEY_FILE="${MUSIC_ENGINE_MUSIC3_API_KEY_FILE:-$PROJECT_ROOT/.runtime/music3-api-key}"
exec "$RUNTIME_ROOT/.venv/bin/python" "$SCRIPT_DIR/music3_api_server.py" \
  --host "$HOST" --port "$PORT"
