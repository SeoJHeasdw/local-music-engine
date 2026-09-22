import math
import struct
import wave
from pathlib import Path

import pytest

from local_music_engine.qc import AudioIntegrityError, artifact_and_findings, inspect_wav


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


def test_truncated_pcm_does_not_pass_integrity(tmp_path: Path):
    path = tmp_path / "truncated.wav"
    write_sine(path)
    path.write_bytes(path.read_bytes()[:-800])
    with pytest.raises(AudioIntegrityError, match="truncated"):
        inspect_wav(path)


def test_partial_stereo_frame_is_rejected(tmp_path: Path):
    path = tmp_path / "partial.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0" * 399)
    with pytest.raises(AudioIntegrityError, match="incomplete frame"):
        inspect_wav(path)


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_pcm_widths_have_correct_peak_rms_and_silence(tmp_path: Path, width: int):
    path = tmp_path / "pcm.wav"
    maximum = 1 << (8 * width - 1)
    def encode(value: int) -> bytes:
        return bytes([value + 128]) if width == 1 else value.to_bytes(width, "little", signed=True)
    # 1 second of silence, then 1 second at half amplitude.
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(width)
        wav.setframerate(8000)
        wav.writeframes(encode(0) * 8000 + encode(maximum // 2) * 8000)
    audio = inspect_wav(path)
    assert audio["peak"] == 0.5
    assert audio["rms"] == pytest.approx(math.sqrt(0.125), abs=0.01)
    assert audio["silentFraction"] == 0.5
    assert audio["regions"]["silence"] == [{"startSeconds": 0, "endSeconds": 1}]


def test_silence_requires_all_channels_and_has_listenable_timestamps(tmp_path: Path):
    path = tmp_path / "regions.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(struct.pack("<hh", 8000, 0) * 8000 + b"\0\0\0\0" * 8000 + struct.pack("<hh", 32767, -32768) * 400)
    artifact, findings = artifact_and_findings(path=path, project_relative_path="regions.wav",
        artifact_kind="candidate-audio", created_by_job_id=None, requested_duration_seconds=2.05)
    silence = [finding for finding in findings if finding["check"] == "silence_region"]
    peaks = [finding for finding in findings if finding["check"] == "peak_region"]
    assert len(silence) == len(peaks) == 1
    assert (silence[0]["startSeconds"], silence[0]["endSeconds"]) == (1, 2)
    assert (peaks[0]["startSeconds"], peaks[0]["endSeconds"]) == (2, 2.05)
    assert artifact["audio"]["peak"] == 1


def test_timeline_is_bounded_but_keeps_total_region_count(tmp_path: Path):
    path = tmp_path / "many-peaks.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(1000)
        wav.writeframes((struct.pack("<h", 32767) * 50 + b"\0\0" * 50) * 100)
    result = inspect_wav(path)
    assert result["regionCounts"]["peak"] == 100
    assert len(result["regions"]["peak"]) == 32
