"""A listening copy of each version, mastered the way a released song is.

The engine's audio is unmastered: about -14 LUFS, 12 dB between true peak and
loudness, and close to mono. A listener compared 22 seconds of 'Night Bus' as made,
the same mastered (glue compression, a little wider, limited to a release level of
-9 LUFS), and the master with its top octave rebuilt, all played back at -14 LUFS
like a streaming service plays music. Both mastered clips were "much cleaner"; the
rebuilt top made no difference, so only the master is kept.

Versions stay unmastered: an edit repaints and splices the engine's own samples, and
mastering those twice would change the song outside the edit. The listening copy is a
separate artifact that the app plays and that export writes. An edit's copy reuses
the drive of the version it edits, so the song keeps one level.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

MASTERING_VERSION = 2
TARGET_LUFS = -9.0
# The limiter runs at four times the sample rate so it sees the peaks between samples.
# Limiting at the sample rate left 20 of 73 saved songs above -1 dBTP (up to +0.7);
# oversampled at -1.1 dBFS they measure -1.0 dBTP or lower.
LIMIT = 0.88
OVERSAMPLING = 4
MINIMUM_SECONDS = 3.0      # integrated loudness needs gated blocks to mean anything
SILENT_LUFS = -60.0
DRIVE_RANGE_DB = (-12.0, 24.0)
DRIVE_STEPS = 8            # regula falsi steps after the bracket


class MasteringError(RuntimeError):
    """The listening copy could not be made; the version itself is unaffected."""


def chain(drive_db: float, channels: int, sample_rate: int) -> str:
    widen = "stereotools=slev=1.35," if channels == 2 else ""
    return ("highpass=f=25,acompressor=threshold=0.1:ratio=2.5:attack=20:release=200:knee=4,"
            f"{widen}volume={drive_db:.2f}dB,aresample={sample_rate * OVERSAMPLING},"
            f"alimiter=limit={LIMIT}:attack=5:release=60:level=0:latency=1,aresample={sample_rate}")


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise MasteringError("FFmpeg is required to master the listening copy")
    return path


def ffmpeg_version() -> str:
    result = subprocess.run([_ffmpeg(), "-hide_banner", "-version"], capture_output=True, text=True, check=False)
    return result.stdout.splitlines()[0] if result.stdout else "unknown"


def _summary(stderr: str) -> dict[str, float | None]:
    tail = stderr[stderr.rfind("Summary:"):]
    if not tail:
        raise MasteringError("FFmpeg did not report a loudness summary")

    def value(label: str) -> float | None:
        match = re.search(rf"{label}:\s+(-?[\d.]+|-inf)", tail)
        if not match or match.group(1) == "-inf":
            return None
        return float(match.group(1))

    return {"integratedLufs": value("I"), "loudnessRangeLu": value("LRA"), "truePeakDbtp": value("Peak")}


def measure(path: Path | str, filters: str | None = None) -> dict[str, float | None]:
    graph = f"{filters}," if filters else ""
    result = subprocess.run([_ffmpeg(), "-hide_banner", "-nostdin", "-i", str(path), "-af", f"{graph}ebur128=peak=true",
                             "-f", "null", "-"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MasteringError(f"FFmpeg could not measure {Path(path).name}: {result.stderr.strip()[-300:]}")
    return _summary(result.stderr)


def find_drive(source: Path | str, channels: int, sample_rate: int, target_lufs: float = TARGET_LUFS) -> float:
    """The gain into the limiter that brings the mastered song to the target loudness.

    Loudness rises with drive, never by more than the drive itself (the limiter only takes
    away), so the gap at zero drive is a lower bound. Regula falsi (Illinois) from there
    usually lands within 0.05 LU in four or five FFmpeg passes.
    """

    low, high = DRIVE_RANGE_DB

    def error(drive: float) -> float:
        loudness = measure(source, chain(drive, channels, sample_rate))["integratedLufs"]
        return (SILENT_LUFS if loudness is None else loudness) - target_lufs

    a = min(high, max(low, -error(0.0)))
    fa = error(a)
    if fa >= 0 or a == high:
        return round(a, 2)
    b = min(high, a + 6.0)
    fb = error(b)
    if fb <= 0 and b < high:
        b, fb = high, error(high)
    if fb <= 0:
        return round(high, 2)
    side = 0
    drive = b
    for _ in range(DRIVE_STEPS):
        drive = b - fb * (b - a) / (fb - fa)
        fd = error(drive)
        if abs(fd) <= 0.05:
            break
        if fd > 0:
            b, fb = drive, fd
            if side == 1:
                fa /= 2
            side = 1
        else:
            a, fa = drive, fd
            if side == -1:
                fb /= 2
            side = -1
    return round(drive, 2)


def master_audio(source: Path | str, destination: Path | str, *, channels: int, sample_rate: int,
                 duration_seconds: float, drive_db: float | None = None, on_staged: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Write the mastered copy to a new file and return what was done and measured.

    ``drive_db`` given means the copy follows an existing level (an edit of a mastered
    version); otherwise it is searched for. ``on_staged`` receives the durable temporary
    file before publication, as audio finishing does, so a crash leaves owned outputs.
    """

    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    if channels not in (1, 2):
        raise MasteringError("only mono and stereo audio is mastered")
    if not math.isfinite(duration_seconds) or duration_seconds < MINIMUM_SECONDS:
        raise MasteringError(f"audio shorter than {MINIMUM_SECONDS:g} s has no stable loudness to master to")
    before = measure(source)
    if before["integratedLufs"] is None or before["integratedLufs"] < SILENT_LUFS:
        raise MasteringError("audio is silent; there is nothing to master")
    inherited = drive_db is not None
    if drive_db is None:
        drive_db = find_drive(source, channels, sample_rate)
    elif not math.isfinite(drive_db) or not DRIVE_RANGE_DB[0] <= drive_db <= DRIVE_RANGE_DB[1]:
        raise MasteringError("inherited drive is out of range")
    filters = chain(drive_db, channels, sample_rate)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp.wav", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    staged = False
    try:
        result = subprocess.run([_ffmpeg(), "-hide_banner", "-nostdin", "-y", "-i", str(source), "-af", filters,
                                 "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
                                 "-c:a", "pcm_s24le", "-f", "wav", str(temporary)], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise MasteringError(f"FFmpeg could not master the copy: {result.stderr.strip()[-300:]}")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        identity = temporary.stat()
        staged_output = {"path": str(destination.absolute()), "temporaryPath": str(temporary.absolute()),
                         "device": identity.st_dev, "inode": identity.st_ino,
                         "expectedBytes": identity.st_size, "expectedSha256": digest}
        if on_staged is not None:
            staged = True
            on_staged(dict(staged_output))
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if not staged:
            temporary.unlink(missing_ok=True)
    after = measure(destination)
    return {"version": MASTERING_VERSION, "targetLufs": TARGET_LUFS, "driveDb": drive_db, "driveInherited": inherited,
            "chain": filters, "ffmpeg": ffmpeg_version(), "source": before, "output": after}
