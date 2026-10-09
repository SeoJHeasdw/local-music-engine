"""Validate source-bound vocal/backing measurements without loading a model."""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .music_structure import MAX_AUDIO_SECONDS, MusicStructureError, _decode
from .storage import sha256_file


class RhythmEvidenceError(ValueError):
    pass


def _db(rms: float) -> float | None:
    return 20 * math.log10(rms) if rms > 0 else None


def _rms_and_matching_windows(audio: Path, reference: Path, window_seconds: float) -> tuple[list[float], list[bool], float]:
    """Transfer cached observations only across byte-identical PCM windows."""
    try:
        with wave.open(str(audio), "rb") as target, wave.open(str(reference), "rb") as source:
            metadata = lambda wav: (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes(), wav.getcomptype())
            if metadata(target) != metadata(source):
                raise RhythmEvidenceError("Cached observation source and candidate PCM metadata differ")
            channels, width, rate, frames, compression = metadata(source)
            duration = frames / rate
            if compression != "NONE" or width not in (1, 2, 3, 4) or channels not in (1, 2) or not 0 < duration <= MAX_AUDIO_SECONDS:
                raise RhythmEvidenceError("Unsupported source audio for cached stem measurements")
            step = max(1, round(rate * window_seconds))
            values, matching = [], []
            decoded = 0
            while data := source.readframes(step):
                other = target.readframes(step)
                samples = _decode(data, width, channels)
                count = len(samples)
                if len(_decode(other, width, channels)) != count:
                    raise RhythmEvidenceError("Candidate PCM is truncated")
                if count != step and decoded + count != frames:
                    raise RhythmEvidenceError("Cached observation source PCM is truncated")
                core_start = decoded
                decoded += count
                if rate != 44100:
                    # The existing separator measured 44.1 kHz resampled audio.
                    # Match that energy domain with phase-aligned filter context;
                    # comparing original-rate RMS would incorrectly reject it.
                    from scipy.signal import resample_poly
                    divisor = math.gcd(rate, 44100)
                    down = rate // divisor
                    read_start = max(0, (core_start // down - 2) * down)
                    read_end = min(frames, ((decoded + down - 1) // down + 2) * down)
                    source.setpos(read_start)
                    context = _decode(source.readframes(read_end - read_start), width, channels).astype(np.float32)
                    resampled = resample_poly(context, 44100 // divisor, down, axis=0)
                    begin = round((core_start - read_start) * 44100 / rate)
                    end = begin + round(count * 44100 / rate)
                    energy_samples = resampled[begin:end]
                    source.setpos(decoded)
                else:
                    energy_samples = samples.astype(np.float32)
                values.append(float(np.sqrt(np.mean(energy_samples * energy_samples))))
                matching.append(data == other)
            if decoded != frames or target.readframes(1):
                raise RhythmEvidenceError("Cached observation source PCM is incomplete")
            return values, matching, duration
    except (OSError, wave.Error, EOFError, MusicStructureError) as error:
        raise RhythmEvidenceError(f"Cannot validate cached stem PCM: {error}") from error


def _matching_ranges(matching: list[bool], step: float, duration: float) -> list[dict[str, float]]:
    ranges = []
    start = None
    for index, valid in enumerate([*matching, False]):
        if valid and start is None:
            start = index * step
        elif not valid and start is not None:
            ranges.append({"startSeconds": start, "endSeconds": min(duration, index * step)})
            start = None
    return ranges


def load_cached_stem_evidence(cache_path: Path | str, *, audio: Path | str, label: str | None = None,
                             evidence_audio: Path | str | None = None) -> dict[str, Any]:
    """Read an explicit cache and audit its relation to the candidate.

    Legacy caches did not freeze the source hash. All cached mix RMS measurements
    must therefore match the explicitly named, currently hashed source. This is
    disclosed as weaker historical provenance; it is not a new separator run.
    A differently spliced candidate can use only byte-identical PCM windows.
    """
    cache, target = Path(cache_path).expanduser().resolve(), Path(audio).expanduser().resolve()
    if not cache.is_file() or cache.stat().st_size > 4_000_000:
        raise RhythmEvidenceError("Stem evidence must be an existing JSON file below 4 MB")
    cache_hash, target_hash = sha256_file(cache), sha256_file(target)
    try:
        content = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RhythmEvidenceError(f"Cannot read stem evidence: {error}") from error
    if label is not None:
        if not isinstance(content, dict) or label not in content:
            raise RhythmEvidenceError("Requested stem evidence label does not exist")
        content = content[label]
    if not isinstance(content, dict):
        raise RhythmEvidenceError("Stem evidence must be an object")
    reference = Path(evidence_audio).expanduser().resolve() if evidence_audio is not None else target
    source_hash = sha256_file(reference)
    # Native source-bound evidence is emitted by the regular quality worker.
    if "separation" in content:
        content = content["separation"]
        if not isinstance(content, dict):
            raise RhythmEvidenceError("Separated-energy result must contain an object")
    if "frameEvidence" in content:
        evidence = content["frameEvidence"]
        if not isinstance(evidence, dict) or evidence.get("sourceArtifactSha256") != source_hash:
            raise RhythmEvidenceError("Separated frame evidence source hash does not match the named audio")
        step = evidence.get("windowSeconds")
        series = evidence.get("frameSeries", {})
        columns, rows = series.get("columns"), series.get("points")
        required = ["startSeconds", "endSeconds", "mixRms", "vocalRms", "accompanimentRms"]
        if not isinstance(columns, list) or not all(name in columns for name in required) or not isinstance(rows, list):
            raise RhythmEvidenceError("Separated frame evidence lacks required aligned energy columns")
        positions = [columns.index(name) for name in required]
        try:
            records = [[row[position] for position in positions] for row in rows]
        except (TypeError, IndexError) as error:
            raise RhythmEvidenceError("Separated frame evidence rows are invalid") from error
        legacy = False
        model = content.get("model", "estimated vocal/residual backing")
    else:
        step = content.get("window")
        if evidence_audio is not None and isinstance(content.get("path"), str) and Path(content["path"]).expanduser().resolve() != reference:
            raise RhythmEvidenceError("Legacy cache names a different explicitly selected source audio")
        arrays = [content.get(key) for key in ("mix", "vocal", "accomp")]
        if any(not isinstance(values, list) for values in arrays) or len({len(values) for values in arrays}) != 1:
            raise RhythmEvidenceError("Legacy stem evidence requires equal mix, vocal and accompaniment arrays")
        if not isinstance(step, (int, float)) or isinstance(step, bool) or not math.isfinite(step) or not 0.01 <= step <= 1:
            raise RhythmEvidenceError("Stem evidence window must be between 0.01 and 1 second")
        records = [[index * step, (index + 1) * step, *values] for index, values in enumerate(zip(*arrays))]
        legacy, model = True, "historical UMXHQ vocal/residual backing estimates"
    if not isinstance(step, (int, float)) or isinstance(step, bool) or not math.isfinite(step) or not 0.01 <= step <= 1:
        raise RhythmEvidenceError("Stem evidence window must be between 0.01 and 1 second")
    if not 1 <= len(records) <= 61_000:
        raise RhythmEvidenceError("Stem frame evidence is empty or exceeds its bounded duration")
    measured, matching, duration = _rms_and_matching_windows(target, reference, float(step))
    if len(records) > len(measured) or duration - len(records) * step > step + 1e-6:
        raise RhythmEvidenceError("Stem frame evidence does not cover the named source duration")
    for index, record in enumerate(records):
        if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in record)
                or any(value < 0 for value in record) or record[1] <= record[0]
                or abs(record[0] - index * step) > 1e-4 or abs(record[1] - min(duration, (index + 1) * step)) > 1e-4):
            raise RhythmEvidenceError("Stem frame energy/timing is invalid or misaligned")
        if not math.isclose(record[2], measured[index], rel_tol=3e-5, abs_tol=3e-7):
            raise RhythmEvidenceError("Cached mix energy differs from the currently hashed source")
    matching = matching[:len(records)]
    if not any(matching):
        raise RhythmEvidenceError("Candidate has no PCM windows identical to the cached analysis source")
    provenance = {"cacheSha256": cache_hash, "sourceArtifactSha256": source_hash,
                  "targetArtifactSha256": target_hash, "binding": "legacy_source_mix_rms_validated" if legacy else "source_sha256_and_mix_rms_verified",
                  "legacySourceHashMissing": legacy, "validPcmRanges": _matching_ranges(matching, float(step), duration),
                  "modelRunNow": False}
    evidence_source = {"method": str(model), "reliability": 0.65 if legacy else 0.75,
                       "provenance": provenance, "caveat": "Estimated stems can leak or remove instruments; historical source hashes were not recorded." if legacy else "Estimated stems can leak or remove instruments."}
    backing, vocals = [], []
    for valid, (start, end, mix, vocal, accompaniment) in zip(matching, records):
        if valid:
            time = (start + end) / 2
            backing.append([time, _db(accompaniment), _db(mix)])
            vocals.append([time, _db(vocal)])
    # Exact gaps remain visible in timestamps. A detector must never join across
    # non-matching windows or a splice and present copied observations as current.
    if sha256_file(target) != target_hash or sha256_file(reference) != source_hash or sha256_file(cache) != cache_hash:
        raise RhythmEvidenceError("Audio or stem evidence changed during validation")
    result = {"accompaniment": {"source": {**evidence_source, "kind": "separated_accompaniment"},
                "validatedRanges": provenance["validPcmRanges"],
                "windowSeconds": step, "frameSeries": {"columns": ["timeSeconds", "rmsDbfs", "mixRmsDbfs"], "points": backing}},
            "vocal": {"source": {**evidence_source, "kind": "separated_vocal"},
                "validatedRanges": provenance["validPcmRanges"],
                "windowSeconds": step, "frameSeries": {"columns": ["timeSeconds", "rmsDbfs"], "points": vocals}},
            "provenance": provenance}
    percussion = content.get("percussionEvidence")
    if isinstance(percussion, dict) and percussion.get("sourceArtifactSha256") == target_hash and source_hash == target_hash:
        result["percussion"] = percussion
    return result


def separator_frame_inputs(separation: dict[str, Any] | None, *, source_sha256: str) -> dict[str, Any]:
    """Adapt freshly measured source-bound worker data, never summary-only data."""
    if not isinstance(separation, dict):
        return {}
    evidence = separation.get("frameEvidence")
    if not isinstance(evidence, dict) or evidence.get("sourceArtifactSha256") != source_sha256:
        return {}
    try:
        columns = evidence["frameSeries"]["columns"]
        rows = evidence["frameSeries"]["points"]
        positions = [columns.index(name) for name in ("startSeconds", "endSeconds", "mixRms", "vocalRms", "accompanimentRms")]
        records = [[row[position] for position in positions] for row in rows]
        if any(any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in record)
               or record[1] <= record[0] for record in records):
            return {}
        source = {"method": separation.get("model", "UMXHQ vocal/residual backing"), "reliability": 0.75,
                  "sourceArtifactSha256": source_sha256, "caveat": "Estimated stems can leak or remove instruments."}
        return {"accompaniment": {"source": {**source, "kind": "separated_accompaniment"},
                    "windowSeconds": evidence["windowSeconds"], "frameSeries": {"columns": ["timeSeconds", "rmsDbfs", "mixRmsDbfs"],
                    "points": [[(start + end) / 2, _db(backing), _db(mix)] for start, end, mix, vocal, backing in records]}},
                "vocal": {"source": {**source, "kind": "separated_vocal"},
                    "windowSeconds": evidence["windowSeconds"], "frameSeries": {"columns": ["timeSeconds", "rmsDbfs"],
                    "points": [[(start + end) / 2, _db(vocal)] for start, end, mix, vocal, backing in records]}}}
    except (KeyError, TypeError, ValueError, IndexError):
        return {}
