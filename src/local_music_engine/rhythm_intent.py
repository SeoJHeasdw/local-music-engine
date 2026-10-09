"""Bounded, source-bound local musical intent with an immutable input snapshot.

An explicit section describes expected expression, never certifies audio quality.
Loading an intent file reads metadata and hashes only; it cannot change a project,
run a model, or infer intent from personal files.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
import wave

from .rhythm_diagnostics import validate_expected_sections
from .storage import sha256_file

VERSION = "rhythm-intent-v1"
MAX_FILE_BYTES = 64_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RhythmIntent:
    """The validated bytes' meaning; later edits to the file cannot alter it."""

    source_artifact_sha256: str
    intent_file_sha256: str
    sections: tuple[Mapping[str, Any], ...]
    version: str = VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "sourceArtifactSha256": self.source_artifact_sha256,
                "intentFileSha256": self.intent_file_sha256,
                "sections": [dict(section) for section in self.sections]}


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Rhythm intent JSON cannot contain duplicate keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Rhythm intent JSON must contain finite numbers")


def load_rhythm_intent(path: Path | str, *, audio: Path | str,
                       source_sha256: str | None = None) -> RhythmIntent:
    """Validate all input and the target WAV before any saved inspection mutation."""
    with Path(path).expanduser().open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Rhythm intent JSON is too large")
    try:
        supplied = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Rhythm intent must be valid UTF-8 JSON") from error
    required = {"version", "sourceArtifactSha256", "sections"}
    if not isinstance(supplied, dict) or set(supplied) != required:
        raise ValueError("Rhythm intent must contain only version, sourceArtifactSha256 and sections")
    if supplied["version"] != VERSION:
        raise ValueError("Unsupported rhythm intent version")
    if not isinstance(supplied["sections"], list):
        raise ValueError("Rhythm intent sections must be a list")
    claimed_hash = supplied["sourceArtifactSha256"]
    if not isinstance(claimed_hash, str) or _SHA256.fullmatch(claimed_hash) is None:
        raise ValueError("Rhythm intent requires a lowercase SHA-256 source hash")
    audio = Path(audio).expanduser().resolve()
    actual_hash = sha256_file(audio)
    if claimed_hash != actual_hash or source_sha256 is not None and actual_hash != source_sha256:
        raise ValueError("Rhythm intent source hash does not match the target audio")
    try:
        with wave.open(str(audio), "rb") as stream:
            frames, rate = stream.getnframes(), stream.getframerate()
    except (wave.Error, EOFError) as error:
        raise ValueError("Rhythm intent requires a readable target WAV") from error
    if rate <= 0 or frames <= 0:
        raise ValueError("Rhythm intent requires a nonempty target WAV")
    normalized = validate_expected_sections(supplied["sections"], frames / rate)
    if sha256_file(audio) != actual_hash:
        raise ValueError("Audio changed while checking rhythm intent")
    return RhythmIntent(actual_hash, hashlib.sha256(raw).hexdigest(),
                        tuple(MappingProxyType(dict(section)) for section in normalized))
