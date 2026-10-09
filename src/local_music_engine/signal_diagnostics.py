"""Read-only, source-bound observations of abrupt interior digital silence.

Near-zero PCM with active flanks is a technical signal observation. It does not
prove a model fault: digital silence can be deliberate musical expression.
Only PCM chunks of at most one second and two half-second flanks are decoded;
no models, audio changes, whole-track prompt interpretation or retries occur.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
import wave

import numpy as np

from .music_structure import MAX_AUDIO_SECONDS, MusicStructureError, _decode
from .rhythm_diagnostics import validate_expected_sections
from .storage import sha256_file

VERSION = "signal-dropouts-v1"
MIN_GAP_SECONDS = 0.08
MAX_GAP_SECONDS = 4.0
FLANK_SECONDS = 0.5
EDGE_SECONDS = 1.0
BOUNDARY_SECONDS = 0.02
MIN_FLANK_DBFS = -45.0
MIN_RELATIVE_DEPTH_DB = 45.0
MIN_BOUNDARY_ENERGY_RATIO = 0.5


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


def _db(rms: float) -> float:
    return 20 * math.log10(max(rms, 1e-12))


def _read(audio: wave.Wave_read, start: int, count: int, width: int, channels: int) -> np.ndarray:
    audio.setpos(start)
    samples = _decode(audio.readframes(count), width, channels)
    if len(samples) != count:
        raise MusicStructureError("WAV PCM is truncated during digital dropout analysis")
    return samples


def inspect_signal_dropouts(path: Path | str, *, expected_sections: Any = None) -> dict[str, Any]:
    """Measure fully supported interior near-zero runs without inferring intent."""
    path = Path(path).expanduser().resolve()
    source_hash = sha256_file(path)
    try:
        audio = wave.open(str(path), "rb")
    except (wave.Error, EOFError) as error:
        raise MusicStructureError("Digital dropout analysis requires a readable PCM WAV") from error
    with audio:
        channels, width, rate, frames = audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()
        if audio.getcomptype() != "NONE" or width not in {1, 2, 3, 4}:
            raise MusicStructureError("Digital dropout analysis supports only uncompressed PCM WAV")
        if not 1 <= channels <= 32 or not 1 <= rate <= 384_000 or frames <= 0:
            raise MusicStructureError("WAV has invalid or unsupported channel, rate, or frame metadata")
        duration = frames / rate
        if duration > MAX_AUDIO_SECONDS:
            raise MusicStructureError(f"Digital dropout analysis supports at most {MAX_AUDIO_SECONDS:g} seconds")
        sections = validate_expected_sections(expected_sections, duration)
        near_zero = min(1 / (1 << (width * 8 - 1)), 1 / 32768)
        candidates = []
        run_start: int | None = None
        run_energy, run_peak = 0.0, 0.0
        run_first, run_last = np.zeros(channels), np.zeros(channels)
        zero_runs, eligible_runs, active_chunks = 0, 0, 0

        def close_run(stop: int) -> None:
            nonlocal run_start, run_energy, run_peak, zero_runs, eligible_runs
            if run_start is None:
                return
            zero_runs += 1
            gap_seconds = (stop - run_start) / rate
            if (MIN_GAP_SECONDS <= gap_seconds <= MAX_GAP_SECONDS and run_start / rate >= EDGE_SECONDS
                    and (frames - stop) / rate >= EDGE_SECONDS):
                candidates.append({"first": run_start, "stop": stop, "energy": run_energy, "peak": run_peak,
                                   "firstSample": run_first.copy(), "lastSample": run_last.copy()})
                eligible_runs += 1
            run_start, run_energy, run_peak = None, 0.0, 0.0

        offset = 0
        while offset < frames:
            count = min(rate, frames - offset)
            samples = _decode(audio.readframes(count), width, channels)
            if len(samples) != count:
                raise MusicStructureError("WAV PCM is truncated during digital dropout analysis")
            active_chunks += _db(_rms(samples)) >= MIN_FLANK_DBFS
            mask = np.max(np.abs(samples), axis=1) <= near_zero
            changes = np.flatnonzero(mask[1:] != mask[:-1]) + 1
            boundaries = np.r_[0, changes, len(mask)]
            for left, right in zip(boundaries[:-1], boundaries[1:]):
                if mask[left]:
                    region = samples[left:right]
                    if run_start is None:
                        run_start = offset + int(left)
                        run_first = region[0].copy()
                    run_energy += float(np.sum(region * region))
                    run_peak = max(run_peak, float(np.max(np.abs(region))))
                    run_last = region[-1].copy()
                else:
                    close_run(offset + int(left))
            offset += count
        close_run(frames)

        events = []
        rejected_flanks, rejected_fades = 0, 0
        flank_frames = max(1, round(rate * FLANK_SECONDS))
        boundary_frames = max(1, round(rate * BOUNDARY_SECONDS))
        for candidate in candidates:
            first, stop = candidate["first"], candidate["stop"]
            before = _read(audio, first - flank_frames, flank_frames, width, channels)
            after = _read(audio, stop, flank_frames, width, channels)
            before_rms, after_rms = _rms(before), _rms(after)
            gap_rms = math.sqrt(candidate["energy"] / ((stop - first) * channels))
            before_db, after_db, gap_db = _db(before_rms), _db(after_rms), _db(gap_rms)
            depth = min(before_db, after_db) - gap_db
            if min(before_db, after_db) < MIN_FLANK_DBFS or depth < MIN_RELATIVE_DEPTH_DB:
                rejected_flanks += 1
                continue
            boundary_before, boundary_after = _rms(before[-boundary_frames:]), _rms(after[:boundary_frames])
            ratios = (boundary_before / max(before_rms, 1e-12), boundary_after / max(after_rms, 1e-12))
            if min(ratios) < MIN_BOUNDARY_ENERGY_RATIO:
                rejected_fades += 1
                continue
            start, end = first / rate, stop / rate
            declared_rest = next((section for section in sections if section.get("expectedRest") is True
                and section["startSeconds"] - 0.0001 <= start and end <= section["endSeconds"] + 0.0001), None)
            observed = {"kind": "technicalSignalObservation", "gapDurationSeconds": round(end - start, 6),
                "beforeRmsDbfs": round(before_db, 3), "duringRmsDbfs": round(gap_db, 3),
                "afterRmsDbfs": round(after_db, 3), "relativeDepthDb": round(depth, 3),
                "gapPeak": round(candidate["peak"], 10), "zeroThreshold": near_zero,
                "beforeBoundaryRmsDbfs": round(_db(boundary_before), 3),
                "afterBoundaryRmsDbfs": round(_db(boundary_after), 3),
                "beforeBoundaryEnergyRatio": round(ratios[0], 4), "afterBoundaryEnergyRatio": round(ratios[1], 4),
                "beforeBoundaryJump": round(float(np.max(np.abs(before[-1] - candidate["firstSample"]))), 8),
                "afterBoundaryJump": round(float(np.max(np.abs(after[0] - candidate["lastSample"]))), 8),
                "intent": "unknown", "intentAssessment": "consistent_with_declared_silence" if declared_rest else "unknown"}
            if declared_rest is not None:
                observed["declaredRestRangeSeconds"] = [declared_rest["startSeconds"], declared_rest["endSeconds"]]
            events.append({"check": "mix_dropout_suspected", "category": "mix_dropout",
                "kind": "technicalSignalObservation", "severity": "info" if declared_rest else "warning",
                "confidence": 1.0, "startSeconds": round(start, 6), "endSeconds": round(end, 6),
                "evidenceStatus": "consistent_with_declared_silence" if declared_rest else "intent_unknown",
                "observed": observed, "threshold": {"minimumGapSeconds": MIN_GAP_SECONDS,
                    "maximumGapSeconds": MAX_GAP_SECONDS, "minimumFlankRmsDbfs": MIN_FLANK_DBFS,
                    "minimumRelativeDepthDb": MIN_RELATIVE_DEPTH_DB, "minimumBoundaryEnergyRatio": MIN_BOUNDARY_ENERGY_RATIO,
                    "flankSeconds": FLANK_SECONDS, "boundarySeconds": BOUNDARY_SECONDS, "edgeExclusionSeconds": EDGE_SECONDS},
                "message": "The full mix reaches near-digital silence abruptly and rebounds inside active audio; a declared local rest explains this observation, without confirming musical quality." if declared_rest else
                    "The full mix reaches near-digital silence abruptly and rebounds inside active audio; this may be an intentional silence or a signal dropout and requires listening.",
                "retryEligible": False})
    if sha256_file(path) != source_hash:
        raise ValueError("Audio changed during read-only digital dropout inspection")
    status = "needs_review" if any(event["severity"] == "warning" for event in events) else "observed" if active_chunks and duration >= EDGE_SECONDS * 2 + MIN_GAP_SECONDS else "unknown"
    check = {"status": status, "source": {"kind": "full_mix_pcm", "method": "streamed all-channel near-zero PCM with active flanks and boundary energy",
             "reliability": 1.0, "sourceArtifactSha256": source_hash},
             "observed": {"zeroRunCount": zero_runs, "interiorCandidateRunCount": eligible_runs,
                          "measuredDropoutCount": len(events), "rejectedQuietFlankCount": rejected_flanks,
                          "rejectedSmoothBoundaryCount": rejected_fades, "activeChunkCount": active_chunks,
                          "maximumDecodedChunkSeconds": 1.0, "allEventsPreserved": True},
             "reason": "Only this precise digital-silence pattern was checked; another defect or intentional expression remains possible."}
    return {"version": VERSION, "sourceArtifactSha256": source_hash, "durationSeconds": duration,
            "check": check, "events": events, "totalEventCount": len(events),
            "confidenceMeaning": "Strength of an exact PCM signal observation, not probability of a model fault.",
            "limitations": ["Digital silence can be intentional; local declarations do not certify musical quality.",
                            "Gradual fades, ordinary quiet audio and nonzero gain attenuation are outside this check.",
                            "This check does not inspect semantic vocals, lyrics, harmony or arrangement quality."]}
