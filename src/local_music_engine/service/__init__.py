"""Job service: the engine's queue for arkAgent and scripts, without the app.

Speaks the shared engine contract (arkAgent ``docs/ENGINE-CONTRACT.md``, v0) on
127.0.0.1::

    uv run music-engine serve
"""

from pathlib import Path

ENGINE = "music"
CONTRACT_VERSION = 0
DEFAULT_PORT = 47833
ROOT = Path(__file__).resolve().parents[3]


def default_output_dir() -> Path:
    return ROOT / "output"
