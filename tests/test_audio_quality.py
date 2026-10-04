import math
import hashlib
import os
import stat
import struct
import wave
from pathlib import Path

import pytest

from local_music_engine import audio_quality

from local_music_engine.audio_quality import (
    AudioFinishPolicy,
    AudioQualityError,
    AudioQualityPolicy,
    analyze_audio_quality,
    finish_audio,
)


def write_pcm(path: Path, values: list[int], *, channels: int = 1, width: int = 2, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(b"".join(bytes([value + 128]) if width == 1 else value.to_bytes(width, "little", signed=True) for value in values))


def read_pcm(path: Path) -> tuple[tuple[int, int, int, int], bytes]:
    with wave.open(str(path), "rb") as audio:
        return (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()), audio.readframes(audio.getnframes())


def sine(*, seconds: float = 1, rate: int = 8000, amplitude: int = 12000, offset: int = 0) -> list[int]:
    return [round(offset + amplitude * math.sin(2 * math.pi * 220 * index / rate)) for index in range(round(seconds * rate))]


def checks(result: dict) -> set[str]:
    return {finding["check"] for finding in result["findings"]}


def test_regular_audio_is_not_regenerated_for_boundary_or_loudness(tmp_path: Path) -> None:
    path = tmp_path / "normal.wav"
    write_pcm(path, sine())
    result = analyze_audio_quality(path, requested_duration_seconds=1)
    assert result["metrics"]["frames"] == 8000
    assert result["metrics"]["rmsDbfs"] == pytest.approx(-11.74, abs=0.1)
    assert result["metrics"]["crestFactorDb"] == pytest.approx(3.01, abs=0.01)
    assert "abrupt_end" in checks(result)
    assert result["retryEligible"] is False
    assert all(not finding["retryEligible"] for finding in result["findings"])


def test_single_full_scale_peak_is_not_sustained_clipping(tmp_path: Path) -> None:
    path = tmp_path / "transient.wav"
    samples = sine()
    samples[1234] = 32767
    write_pcm(path, samples)
    result = analyze_audio_quality(path)
    assert "full_scale_peak" in checks(result)
    assert "sustained_clipping" not in checks(result)
    assert result["retryEligible"] is False
    assert result["metrics"]["longestFullScaleRunSeconds"] == 1 / 8000


def test_full_scale_run_continues_across_analysis_windows(tmp_path: Path) -> None:
    path = tmp_path / "clipped.wav"
    samples = sine()
    samples[390:430] = [32767] * 40
    write_pcm(path, samples)
    result = analyze_audio_quality(path)
    assert "sustained_clipping" in checks(result)
    assert result["retryEligible"] is True
    assert result["automaticStatus"] == "retry_recommended"
    assert result["metrics"]["longestFullScaleRunSeconds"] == 0.005
    assert result["technicalScore"] < 50


def test_alternating_full_scale_polarity_is_not_a_flat_top(tmp_path: Path) -> None:
    path = tmp_path / "alternating.wav"
    write_pcm(path, [32767, -32768] * 4000)
    result = analyze_audio_quality(path)
    assert "sustained_clipping" not in checks(result)
    assert result["metrics"]["longestFullScaleRunSeconds"] == 1 / 8000


def test_entire_digital_silence_is_retry_eligible(tmp_path: Path) -> None:
    path = tmp_path / "silent.wav"
    write_pcm(path, [0] * 8000, channels=2)
    result = analyze_audio_quality(path)
    assert checks(result) == {"digital_silence"}
    assert result["retryEligible"] is True
    assert result["metrics"]["rmsDbfs"] is None
    assert result["technicalScore"] == 0


def test_long_rest_is_an_observation_and_not_a_regeneration_trigger(tmp_path: Path) -> None:
    path = tmp_path / "rest.wav"
    write_pcm(path, sine() + [0] * 24000 + sine())
    result = analyze_audio_quality(path)
    assert result["metrics"]["longestDigitalSilenceSeconds"] == 3
    assert result["metrics"]["digitalSilenceWindowFraction"] == 0.6
    assert "digital_silence" not in checks(result)
    assert result["retryEligible"] is False


def test_silence_requires_all_channels(tmp_path: Path) -> None:
    path = tmp_path / "one-channel.wav"
    write_pcm(path, [sample for value in sine() for sample in (value, 0)], channels=2)
    result = analyze_audio_quality(path)
    assert result["metrics"]["nearDigitalSilenceSampleFraction"] > 0.5
    assert result["metrics"]["digitalSilenceWindowFraction"] == 0
    assert "digital_silence" not in checks(result)


def test_stereo_cancellation_is_advisory_without_changing_the_mix(tmp_path: Path) -> None:
    path = tmp_path / "side.wav"
    write_pcm(path, [sample for value in sine() for sample in (value, -value)], channels=2)
    result = analyze_audio_quality(path)
    assert result["metrics"]["stereoCorrelation"] == pytest.approx(-1)
    assert result["metrics"]["monoToStereoRmsRatio"] == 0
    assert result["metrics"]["stereoCancellationActiveFraction"] == 1
    assert "stereo_mono_cancellation" in checks(result)
    assert result["retryEligible"] is False


def test_stereo_match_and_silent_mix_do_not_report_cancellation(tmp_path: Path) -> None:
    path = tmp_path / "center.wav"
    write_pcm(path, [sample for value in sine() for sample in (value, value)], channels=2)
    result = analyze_audio_quality(path)
    assert result["metrics"]["stereoCorrelation"] == pytest.approx(1)
    assert result["metrics"]["monoToStereoRmsRatio"] == 1
    assert "stereo_mono_cancellation" not in checks(result)


def test_duration_tolerance_separates_warning_from_retry(tmp_path: Path) -> None:
    path = tmp_path / "duration.wav"
    write_pcm(path, sine())
    mild = analyze_audio_quality(path, requested_duration_seconds=2)
    assert "duration_mismatch" in checks(mild)
    assert mild["retryEligible"] is False
    gross = analyze_audio_quality(path, requested_duration_seconds=10)
    assert gross["retryEligible"] is True
    assert next(item for item in gross["findings"] if item["check"] == "duration_mismatch")["severity"] == "error"


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_supported_pcm_widths_have_consistent_measurements(tmp_path: Path, width: int) -> None:
    maximum = 1 << (8 * width - 1)
    path = tmp_path / f"width-{width}.wav"
    write_pcm(path, [maximum // 4, -maximum // 4] * 4000, width=width)
    result = analyze_audio_quality(path)
    assert result["metrics"]["peak"] == 0.25
    assert result["metrics"]["rms"] == 0.25
    assert result["metrics"]["channelDcOffsets"] == [0]


def test_truncated_or_partial_frames_do_not_pass(tmp_path: Path) -> None:
    path = tmp_path / "truncated.wav"
    write_pcm(path, sine(), channels=2)
    path.write_bytes(path.read_bytes()[:-100])
    with pytest.raises(AudioQualityError, match="truncated"):
        analyze_audio_quality(path)
    write_pcm(path, sine(), channels=2)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(AudioQualityError, match="incomplete frame"):
        analyze_audio_quality(path)


def test_finishing_removes_dc_and_fades_edges_without_touching_source(tmp_path: Path) -> None:
    source, destination = tmp_path / "source.wav", tmp_path / "finished.wav"
    write_pcm(source, sine(offset=2000))
    original = source.read_bytes()
    result = finish_audio(source, destination)
    assert source.read_bytes() == original
    assert read_pcm(source)[0] == read_pcm(destination)[0]
    assert result["changed"] is True
    assert result["processing"]["channelDcOffsetsRemoved"] == pytest.approx([2000 / 32768])
    assert result["processing"]["gain"] == 1
    assert result["processing"]["endFadeSeconds"] == 0.005
    assert abs(result["outputQuality"]["metrics"]["channelDcOffsets"][0]) < 0.001
    assert result["outputQuality"]["metrics"]["lastFrame"] == [0]
    assert result["existingClippingRepaired"] is False


def test_finishing_does_not_boost_quiet_audio_or_change_middle_phrasing(tmp_path: Path) -> None:
    source, destination = tmp_path / "quiet.wav", tmp_path / "quiet-finished.wav"
    samples = sine(amplitude=300)
    write_pcm(source, samples)
    result = finish_audio(source, destination)
    assert result["changed"] is False
    assert result["processing"]["gain"] == 1
    assert read_pcm(destination) == read_pcm(source)


def test_finished_peak_is_limited_even_when_dc_removal_increases_peak(tmp_path: Path) -> None:
    source, destination = tmp_path / "headroom.wav", tmp_path / "headroom-finished.wav"
    write_pcm(source, [-32768] * 80 + [12000] * 7920)
    result = finish_audio(source, destination)
    assert result["processing"]["gain"] < 1
    assert result["outputQuality"]["metrics"]["peak"] <= 0.9801
    assert result["sourceQuality"]["retryEligible"] is True
    assert result["existingClippingRepaired"] is False


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_finish_preserves_format_and_noop_pcm_at_every_width(tmp_path: Path, width: int) -> None:
    maximum = 1 << (8 * width - 1)
    source, destination = tmp_path / "source.wav", tmp_path / "copy.wav"
    write_pcm(source, [maximum // 100, -(maximum // 100)] * 4000, width=width)
    result = finish_audio(source, destination)
    assert result["changed"] is False
    assert read_pcm(source) == read_pcm(destination)


def test_finishing_never_overwrites_source_or_existing_artifact(tmp_path: Path) -> None:
    source, destination = tmp_path / "source.wav", tmp_path / "existing.wav"
    write_pcm(source, sine())
    original = source.read_bytes()
    destination.write_bytes(b"keep existing artifact")
    with pytest.raises(AudioQualityError, match="separate destination"):
        finish_audio(source, source)
    with pytest.raises(FileExistsError):
        finish_audio(source, destination)
    assert source.read_bytes() == original
    assert destination.read_bytes() == b"keep existing artifact"
    assert not list(tmp_path.glob(".*.tmp"))


def test_finishing_uses_short_fades_only_at_measurable_boundaries(tmp_path: Path) -> None:
    source, destination = tmp_path / "tone.wav", tmp_path / "finished.wav"
    samples = sine()
    write_pcm(source, samples)
    result = finish_audio(source, destination)
    original_pcm = read_pcm(source)[1]
    finished_pcm = read_pcm(destination)[1]
    assert result["processing"]["startFadeSeconds"] == 0
    assert result["processing"]["endFadeSeconds"] == 0.005
    assert original_pcm[: 2 * (8000 - 40)] == finished_pcm[: 2 * (8000 - 40)]
    assert struct.unpack("<h", finished_pcm[-2:])[0] == 0


def test_policy_validation_rejects_unbounded_processing() -> None:
    with pytest.raises(AudioQualityError):
        AudioQualityPolicy(window_seconds=math.inf)
    with pytest.raises(AudioQualityError):
        AudioQualityPolicy(duration_warning_seconds=3, duration_retry_seconds=2)
    with pytest.raises(AudioQualityError):
        AudioFinishPolicy(edge_fade_seconds=0.06)
    with pytest.raises(AudioQualityError):
        AudioFinishPolicy(peak_ceiling=1.1)


def test_invalid_requested_duration_is_rejected_before_reading(tmp_path: Path) -> None:
    with pytest.raises(AudioQualityError, match="finite and positive"):
        analyze_audio_quality(tmp_path / "absent.wav", requested_duration_seconds=float("nan"))


def test_finishing_stages_identity_before_publication_and_keeps_commit_anchor(tmp_path: Path, monkeypatch) -> None:
    source, destination = tmp_path / "source.wav", tmp_path / "finished.wav"
    write_pcm(source, sine())
    recorded = []
    synced_directories = []
    original_fsync = os.fsync

    def observe_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synced_directories.append(descriptor)
        original_fsync(descriptor)

    def persist_staging(record):
        temporary = Path(record["temporaryPath"])
        assert temporary.is_file()
        assert not destination.exists()
        assert temporary.stat().st_size == record["expectedBytes"]
        assert hashlib.sha256(temporary.read_bytes()).hexdigest() == record["expectedSha256"]
        assert analyze_audio_quality(temporary)["metrics"]["frames"] == 8000
        assert synced_directories
        recorded.append(record)

    monkeypatch.setattr(audio_quality.os, "fsync", observe_fsync)
    result = finish_audio(source, destination, on_staged=persist_staging)
    assert result["stagedOutput"] == recorded[0]
    anchor = Path(recorded[0]["temporaryPath"])
    assert anchor.exists()
    assert destination.stat().st_ino == anchor.stat().st_ino == recorded[0]["inode"]
    assert destination.stat().st_dev == recorded[0]["device"]
    assert len(synced_directories) >= 2
    # The caller releases this anchor only after the success manifest is durable.
    anchor.unlink()
    assert destination.is_file()


def test_staged_cancellation_preserves_anchor_for_manifest_cleanup(tmp_path: Path) -> None:
    source, destination = tmp_path / "source.wav", tmp_path / "cancelled.wav"
    write_pcm(source, sine())
    original = source.read_bytes()
    recorded = []

    def cancel_staging(record):
        recorded.append(record)
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        finish_audio(source, destination, on_staged=cancel_staging)
    assert source.read_bytes() == original
    assert not destination.exists()
    assert Path(recorded[0]["temporaryPath"]).is_file()


def test_staging_save_error_does_not_publish_and_retains_recovery_anchor(tmp_path: Path) -> None:
    source, destination = tmp_path / "source.wav", tmp_path / "failed.wav"
    write_pcm(source, sine())
    recorded = []

    def failed_save(record):
        recorded.append(record)
        raise OSError("injected manifest save error")

    with pytest.raises(OSError, match="manifest save"):
        finish_audio(source, destination, on_staged=failed_save)
    assert not destination.exists()
    assert Path(recorded[0]["temporaryPath"]).exists()


def test_directory_sync_failure_before_callback_cleans_unowned_staging(tmp_path: Path, monkeypatch) -> None:
    source, destination = tmp_path / "source.wav", tmp_path / "failed-sync.wav"
    write_pcm(source, sine())
    original = source.read_bytes()
    recorded = []
    original_fsync = os.fsync

    def fail_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected staging directory sync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(audio_quality.os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="directory sync"):
        finish_audio(source, destination, on_staged=recorded.append)
    assert recorded == []
    assert source.read_bytes() == original
    assert not destination.exists()
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("with_manifest_owner", [False, True])
def test_cancellation_after_publication_obeys_manifest_ownership(tmp_path: Path, monkeypatch, with_manifest_owner: bool) -> None:
    source, destination = tmp_path / "source.wav", tmp_path / "interrupted.wav"
    write_pcm(source, sine())
    original = source.read_bytes()
    recorded = []
    original_link = os.link

    def interrupted_link(temporary, target):
        original_link(temporary, target)
        raise KeyboardInterrupt()

    monkeypatch.setattr(audio_quality.os, "link", interrupted_link)
    with pytest.raises(KeyboardInterrupt):
        finish_audio(source, destination, on_staged=recorded.append if with_manifest_owner else None)
    assert source.read_bytes() == original
    if with_manifest_owner:
        assert destination.exists()
        anchor = Path(recorded[0]["temporaryPath"])
        assert anchor.exists()
        assert destination.stat().st_ino == anchor.stat().st_ino
    else:
        assert not destination.exists()
        assert not list(tmp_path.glob(".*.tmp"))


def test_standalone_cancellation_preserves_replaced_output(tmp_path: Path, monkeypatch) -> None:
    source, destination = tmp_path / "source.wav", tmp_path / "replaced.wav"
    write_pcm(source, sine())
    original_link = os.link

    def replace_after_publish(temporary, target):
        original_link(temporary, target)
        Path(target).unlink()
        Path(target).write_bytes(b"user replacement")
        raise KeyboardInterrupt()

    monkeypatch.setattr(audio_quality.os, "link", replace_after_publish)
    with pytest.raises(KeyboardInterrupt):
        finish_audio(source, destination)
    assert destination.read_bytes() == b"user replacement"
    assert not list(tmp_path.glob(".*.tmp"))
