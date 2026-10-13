"""Which engines exist, and which one a request gets when it names none.

ACE-Step 1.5 XL turbo is the engine the app runs and the CLI default. Music 3 stays
selectable by name; its model and runtime are not installed until
``scripts/bootstrap_music3.sh`` restores them.
"""

from __future__ import annotations

from .music3 import BASE_URL as MUSIC3_BASE_URL, ENGINE as MUSIC3_ENGINE, MODEL as MUSIC3_MODEL

ACE_ENGINE = "ace-step"
ACE_BASE_URL = "http://127.0.0.1:18001"
# scripts/start_ace_api.sh loads this DiT unless told otherwise; a request for any
# other one is refused rather than silently served by the loaded checkpoint.
ACE_DIT_MODEL = "acestep-v15-xl-turbo"
ACE_CAPABILITIES = {"text2music": True, "cover": True, "repaint": True, "referenceAudio": False}

ENGINES = (ACE_ENGINE, MUSIC3_ENGINE)
DEFAULT_ENGINE = ACE_ENGINE
# Longest song the CLI and the app request, whichever engine runs.
MAX_DURATION_SECONDS = 300


def engine_endpoint(engine: str, base_url: str | None = None, model: str | None = None) -> tuple[str, str]:
    """The server address and model a request uses when the caller names neither."""

    if engine not in ENGINES:
        raise ValueError("unknown music engine")
    music3 = engine == MUSIC3_ENGINE
    return base_url or (MUSIC3_BASE_URL if music3 else ACE_BASE_URL), model or (MUSIC3_MODEL if music3 else ACE_DIT_MODEL)
