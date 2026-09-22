"""Objective WAV integrity measurements; musical judgment stays with the user."""

from __future__ import annotations

import math
import sys
from array import array
import warnings
import wave
from pathlib import Path
from typing import Any

from .storage import new_id, sha256_file, utc_now

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop


class AudioIntegrityError(ValueError):
    pass


def inspect_wav(path: Path | str) -> dict[str, Any]:
    wav_path = Path(path)
    if not wav_path.is_file() or wav_path.stat().st_size == 0:
        raise AudioIntegrityError(f"audio file is missing or empty: {wav_path}")
    try:
        with wave.open(str(wav_path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frames = audio.getnframes()
            compression = audio.getcomptype()
            if compression != "NONE":
                raise AudioIntegrityError(f"compressed WAV is unsupported: {compression}")
            if channels <= 0 or sample_rate <= 0 or frames <= 0:
                raise AudioIntegrityError("WAV has invalid channel, rate, or frame metadata")
            if sample_width not in {1, 2, 3, 4}:
                raise AudioIntegrityError(f"unsupported PCM sample width: {sample_width}")

            maximum = float(1 << (sample_width * 8 - 1))
            peak_integer = 0
            square_sum = 0.0
            sample_count = 0
            silent_samples = 0
            silent_integer_threshold = int(maximum * 0.0001)
            # Fifty-millisecond windows locate observations for listening without
            # guessing words, beats, or whether an intentional rest is a defect.
            window_frames = max(1, round(sample_rate * 0.05))
            decoded_frames = 0
            spans: dict[str, list[dict[str, float]]] = {"silence": [], "peak": []}
            starts: dict[str, int | None] = {"silence": None, "peak": None}

            def close_span(kind: str, end_frame: int) -> None:
                start = starts[kind]
                if start is not None:
                    if kind != "silence" or end_frame - start >= sample_rate * 0.5:
                        spans[kind].append({"startSeconds": start / sample_rate, "endSeconds": end_frame / sample_rate})
                    starts[kind] = None

            while True:
                chunk = audio.readframes(window_frames)
                if not chunk:
                    break
                if len(chunk) % (sample_width * channels):
                    raise AudioIntegrityError("WAV PCM ends in an incomplete frame")
                # WAV stores 8-bit samples unsigned; audioop operates on signed PCM.
                if sample_width == 1:
                    chunk = audioop.bias(chunk, 1, -128)
                chunk_peak = audioop.max(chunk, sample_width)
                peak_integer = max(peak_integer, chunk_peak)
                chunk_rms = audioop.rms(chunk, sample_width)
                chunk_samples = len(chunk) // sample_width
                square_sum += float(chunk_rms * chunk_rms) * chunk_samples
                sample_count += chunk_samples
                integers = array({1: "b", 2: "h", 3: "i", 4: "i"}[sample_width])
                integers.frombytes(audioop.lin2lin(chunk, 3, 4) if sample_width == 3 else chunk)
                if sys.byteorder != "little" and integers.itemsize > 1:
                    integers.byteswap()
                threshold = silent_integer_threshold * (256 if sample_width == 3 else 1)
                silent_samples += sum(abs(value) <= threshold for value in integers)
                for kind, active in (("silence", chunk_peak <= silent_integer_threshold), ("peak", chunk_peak / maximum >= 0.999)):
                    if active:
                        if starts[kind] is None:
                            starts[kind] = decoded_frames
                    else:
                        close_span(kind, decoded_frames)
                decoded_frames += chunk_samples // channels
            for kind in starts:
                close_span(kind, decoded_frames)
            if decoded_frames != frames:
                raise AudioIntegrityError(f"WAV PCM is truncated: expected {frames} frames, decoded {decoded_frames}")
    except (wave.Error, EOFError) as error:
        raise AudioIntegrityError(f"WAV decode failed: {error}") from error

    if sample_count == 0:
        raise AudioIntegrityError("WAV contains no decoded samples")
    return {
        "frames": frames,
        "sampleRate": sample_rate,
        "channels": channels,
        "sampleWidthBytes": sample_width,
        "durationSeconds": frames / sample_rate,
        "peak": min(1.0, peak_integer / maximum),
        "rms": math.sqrt(square_sum / sample_count) / maximum,
        "silentFraction": silent_samples / sample_count,
        "analysisWindowSeconds": window_frames / sample_rate,
        # Bound manifest size for pathological audio. Keep the longest observations
        # and record the full count so omitted regions are never presented as absent.
        "regions": {kind: sorted(sorted(ranges, key=lambda r: r["endSeconds"] - r["startSeconds"], reverse=True)[:32], key=lambda r: r["startSeconds"]) for kind, ranges in spans.items()},
        "regionCounts": {kind: len(ranges) for kind, ranges in spans.items()},
    }


def artifact_and_findings(
    *,
    path: Path,
    project_relative_path: str,
    artifact_kind: str,
    created_by_job_id: str | None,
    requested_duration_seconds: float | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    audio = inspect_wav(path)
    artifact_id = new_id("artifact")
    artifact = {
        "artifactId": artifact_id,
        "kind": artifact_kind,
        "path": project_relative_path,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "createdAt": utc_now(),
        "createdByJobId": created_by_job_id,
        "audio": audio,
    }
    findings: list[dict[str, Any]] = []

    def add(
        check: str,
        severity: str,
        message: str,
        observed: dict[str, Any],
        threshold: dict[str, Any],
        region: dict[str, float] | None = None,
    ) -> None:
        findings.append(
            {
                "findingId": new_id("finding"),
                "artifactId": artifact_id,
                "check": check,
                "severity": severity,
                "message": message,
                "observed": observed,
                "threshold": threshold,
                "confidence": 1.0,
                "startSeconds": region["startSeconds"] if region else None,
                "endSeconds": region["endSeconds"] if region else None,
                "createdAt": utc_now(),
            }
        )

    add(
        "wav_integrity",
        "info",
        "WAV header and PCM samples decoded successfully.",
        {
            "frames": audio["frames"],
            "sampleRate": audio["sampleRate"],
            "channels": audio["channels"],
        },
        {"requiresNonEmptyPcm": True},
    )
    if audio["peak"] >= 0.999:
        add(
            "peak",
            "warning",
            "Peak is at or near full scale; listen for clipping.",
            {"peak": audio["peak"]},
            {"warningAtOrAbove": 0.999},
        )
    else:
        add(
            "peak",
            "info",
            "Peak is below the clipping warning threshold.",
            {"peak": audio["peak"]},
            {"warningAtOrAbove": 0.999},
        )
    if audio["silentFraction"] > 0.2:
        add(
            "silence",
            "warning",
            "A substantial fraction is near digital silence; intentional rests may trigger this.",
            {"silentFraction": audio["silentFraction"]},
            {"warningAbove": 0.2, "absoluteAmplitudeAtOrBelow": 0.0001},
        )
    else:
        add(
            "silence",
            "info",
            "Near-digital-silence fraction is below the review threshold.",
            {"silentFraction": audio["silentFraction"]},
            {"warningAbove": 0.2, "absoluteAmplitudeAtOrBelow": 0.0001},
        )
    if requested_duration_seconds is not None:
        difference = abs(audio["durationSeconds"] - requested_duration_seconds)
        add(
            "duration",
            "warning" if difference > 0.5 else "info",
            (
                "Actual duration differs from the request by more than 0.5 seconds."
                if difference > 0.5
                else "Actual duration is within 0.5 seconds of the request."
            ),
            {
                "actualSeconds": audio["durationSeconds"],
                "requestedSeconds": requested_duration_seconds,
                "absoluteDifferenceSeconds": difference,
            },
            {"warningDifferenceAboveSeconds": 0.5},
        )
    for kind, regions in audio["regions"].items():
        for region in regions:
            add(
                f"{kind}_region", "warning",
                "Near-digital silence; listen to decide if this rest is intentional." if kind == "silence"
                else "Near-full-scale peak in this window; listen for distortion.",
                {"durationSeconds": region["endSeconds"] - region["startSeconds"], "totalRegions": audio["regionCounts"][kind], "shownRegions": len(regions)},
                {"windowSeconds": audio["analysisWindowSeconds"], **({"minimumSeconds": 0.5, "absoluteAmplitudeAtOrBelow": 0.0001} if kind == "silence" else {"peakAtOrAbove": 0.999})},
                region,
            )
    return artifact, findings
