import math
import struct
import wave
from pathlib import Path

from local_music_engine.qc import artifact_and_findings, inspect_wav


def write_sine(path: Path, *, seconds: float = 1.0, sample_rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = [
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 220 * index / sample_rate)))
            for index in range(int(seconds * sample_rate))
        ]
        output.writeframes(b"".join(frames))


def test_inspect_wav_uses_sample_count_for_duration(tmp_path: Path) -> None:
    path = tmp_path / "tone.wav"
    write_sine(path, seconds=1.25)
    result = inspect_wav(path)
    assert result["frames"] == 10_000
    assert result["durationSeconds"] == 1.25
    assert 0.36 < result["peak"] < 0.37
    assert result["silentFraction"] < 0.01


def test_qc_keeps_observation_and_threshold_separate(tmp_path: Path) -> None:
    path = tmp_path / "tone.wav"
    write_sine(path)
    artifact, findings = artifact_and_findings(
        path=path,
        project_relative_path="artifacts/tone.wav",
        artifact_kind="candidate-audio",
        created_by_job_id=None,
        requested_duration_seconds=2.0,
    )
    duration = next(finding for finding in findings if finding["check"] == "duration")
    assert artifact["audio"]["durationSeconds"] == 1.0
    assert duration["severity"] == "warning"
    assert duration["observed"]["actualSeconds"] == 1.0
    assert duration["threshold"]["warningDifferenceAboveSeconds"] == 0.5
