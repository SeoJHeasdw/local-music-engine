#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "Local quality analysis requires Apple Silicon macOS." >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null; then
  echo "Install FFmpeg before preparing local quality analysis." >&2
  exit 1
fi
if ! command -v uv >/dev/null; then
  echo "Install uv before preparing local quality analysis." >&2
  exit 1
fi
BOOTSTRAP_PYTHON="$(uv python find 3.12.12)"
exec "$BOOTSTRAP_PYTHON" - "$PROJECT_ROOT" <<'PY'
import fcntl
import importlib.util
import os
import pathlib
import subprocess
import sys

project = pathlib.Path(sys.argv[1])
runtime = project / ".runtime" / "quality"
runtime.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location("quality_bootstrap_worker", project / "scripts" / "quality_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
children = []

def stop_bootstrap(code):
    # Only children started by this bootstrap are stopped. The setup worker also
    # watches the original CLI PID independently, including during downloads.
    for child in tuple(children):
        if child.poll() is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
    os._exit(code)

def run(arguments, *, stdout=None):
    with subprocess.Popen(arguments, stdout=stdout) as child:
        children.append(child)
        try:
            code = child.wait()
        finally:
            children.remove(child)
        if code:
            raise subprocess.CalledProcessError(code, arguments)

# All competing bootstrap requests wait on the same stable lock inode. Kernel
# release on interruption avoids stale directory locks or duplicate downloads.
with worker.parent_watchdog("setup", on_exit=stop_bootstrap), (runtime / "bootstrap.lock").open("a+b") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    python = runtime / ".venv" / "bin" / "python"
    if not python.exists():
        run(["uv", "venv", "--python", "3.12.12", str(runtime / ".venv")], stdout=sys.stderr)
    run(["uv", "pip", "sync", "--python", str(python), "--require-hashes",
         str(project / "scripts" / "quality-requirements.txt")], stdout=sys.stderr)
    run([str(python), str(project / "scripts" / "quality_worker.py"), "setup"])
PY
