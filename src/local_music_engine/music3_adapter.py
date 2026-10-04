"""Loopback Music 3 transport using the managed server's task/audio protocol."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .ace_adapter import AceApiError, AceStepClient
from .music3 import BASE_URL, ENGINE
from .music3_auth import read_api_key


class Music3Client(AceStepClient):
    credential_reader = staticmethod(read_api_key)
    api_label = "Music 3"

    def __init__(self, base_url: str = BASE_URL, **options: Any):
        super().__init__(base_url, **options)

    def health(self) -> dict[str, Any]:
        response = self._json("GET", "/health")
        data = response.get("data")
        if (not isinstance(data, dict) or data.get("status") != "ok"
                or data.get("engine") != ENGINE or not data.get("models_initialized")):
            raise AceApiError(f"unhealthy Music 3 API response: {response!r}")
        return data

    def submit(self, request: dict[str, Any], source_audio: Path | None = None) -> str:
        source_fields = {"source_audio", "reference_audio", "reference_audio_path", "ref_audio", "src_audio", "coverSource", "repainting_start", "repainting_end", "audio_cover_strength"}
        if source_audio is not None or request.get("task_type") != "text2music" or source_fields.intersection(request):
            raise ValueError("Music 3 supports text-to-music only; cover, repaint and reference audio are unavailable")
        return super().submit(request)

    def create_sample(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("Music 3 has no lyric-draft endpoint; use the local draft rules or configured assistant")
