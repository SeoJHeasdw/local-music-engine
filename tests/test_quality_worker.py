"""Worker contracts run without installing or loading any learned model."""

import importlib.util
import json
import contextlib
from pathlib import Path
import subprocess
import sys
import threading

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "quality_worker.py"
spec = importlib.util.spec_from_file_location("quality_worker", MODULE_PATH)
worker = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(worker)


def segment(text="바람이 불어오는 이 밤", **overrides):
    return {"text": text, "start": 1, "end": 4, "avg_logprob": -0.3,
            "no_speech_prob": 0.01, "compression_ratio": 1.4, "temperature": 0,
            **overrides}


def test_confidence_requires_measurements_and_keeps_unreliable_segments():
    cleaned, confidence = worker.clean_segments([segment(), segment("추가 가사", avg_logprob=-2)])
    assert len(cleaned) == 2
    assert cleaned[0]["reliable"] is True
    assert cleaned[1]["reliable"] is False
    assert "start" in cleaned[0] and "no_speech_prob" in cleaned[0]
    _cleaned, unknown = worker.clean_segments([segment(avg_logprob=None)])
    assert unknown["reliable"] is False


def test_empty_and_invalid_transcripts_are_inconclusive():
    for raw in (None, [], [segment("아")], [segment(end=1)], [segment(start=float("nan"))]):
        _cleaned, confidence = worker.clean_segments(raw)
        assert confidence["reliable"] is False


def test_repetition_and_no_speech_probabilities_do_not_become_reliable():
    for changes in ({"compression_ratio": 4}, {"no_speech_prob": 0.9}):
        cleaned, confidence = worker.clean_segments([segment(**changes)])
        assert not cleaned[0]["reliable"]
        assert not confidence["reliable"]


def test_loudness_uses_input_values_and_handles_digital_silence():
    result = worker.parse_loudness('log output\n{"input_i":"-15.5","input_lra":"3.2",'
                                   '"input_tp":"-0.7","input_thresh":"-25.9","output_i":"-16.0"}\n')
    assert result["status"] == "measured"
    assert result["integratedLufs"] == -15.5
    assert result["truePeakDbtp"] == -0.7
    silent = worker.parse_loudness('{"input_i":"-inf","input_lra":"0.0",'
                                   '"input_tp":"-inf","input_thresh":"-70.0"}')
    assert silent["status"] == "inconclusive"
    assert silent["integratedLufs"] is None
    json.dumps(silent, allow_nan=False)
    assert worker.parse_loudness("not a measurement")["status"] == "unknown"


def test_vocal_activity_is_energy_evidence_and_regions_are_merged():
    windows = [
        {"startSeconds": 0, "endSeconds": 0.25, "vocalRms": 0.0001, "mixRms": 0.1},
        {"startSeconds": 0.25, "endSeconds": 0.5, "vocalRms": 0.1, "mixRms": 0.2},
        {"startSeconds": 0.5, "endSeconds": 0.75, "vocalRms": 0.1, "mixRms": 0.2},
        {"startSeconds": 0.75, "endSeconds": 1, "vocalRms": 0.0001, "mixRms": 0.1},
    ]
    result = worker.vocal_summary(windows, 1)
    assert result["evidenceOnly"] is True
    assert result["vocalActivityFraction"] == 0.5
    assert result["vocalActivityRegions"] == [{"startSeconds": 0.25, "endSeconds": 0.75}]
    assert "qualityScore" not in result


def test_missing_model_analysis_has_one_json_and_never_downloads(tmp_path, monkeypatch):
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(worker, "MODEL_FILES", ((tmp_path / "missing", "https://example.com/model", "abc"),))
    monkeypatch.setattr(worker, "MODELS", tmp_path)
    monkeypatch.setattr(worker, "RUNTIME", tmp_path / "quality")
    monkeypatch.setattr(worker, "loudness", lambda _audio: {"status": "unavailable"})
    monkeypatch.setattr(worker.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("analysis accessed internet"))
    result = worker.analyze(audio)
    assert not result["available"]
    assert not result["reliable"]
    assert result["separation"]["status"] == "unavailable"


def test_analyze_requires_absolute_existing_audio(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        worker.analyze(Path("relative.wav"))
    with pytest.raises(ValueError, match="absolute"):
        worker.analyze(tmp_path / "missing.wav")


def test_separate_only_missing_weights_never_loads_models_or_downloads(tmp_path, monkeypatch):
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(worker, "UMX_DIRECTORY", tmp_path / "missing-model")
    monkeypatch.setattr(worker, "RUNTIME", tmp_path / "quality")
    monkeypatch.setattr(worker, "separation", lambda _audio: pytest.fail("Unverified model loaded"))
    monkeypatch.setattr(worker.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("Analysis downloaded a model"))
    with pytest.raises(ValueError, match="Verified UMXHQ"):
        worker.separate_only(audio)


def test_attributed_drum_features_measure_real_pulses_without_timing_invention():
    import numpy as np
    rate = 44100
    samples = np.zeros((16 * rate, 2), dtype=np.float32)
    rng = np.random.default_rng(1)
    burst = rng.normal(0, 0.15, round(rate * 0.03)).astype(np.float32)
    for time in np.arange(0.3, 15.8, 0.5):
        start = round(time * rate)
        samples[start:start + len(burst)] = np.column_stack((burst, -burst))
    features, _ = worker.drum_frame_features(samples, offset=0, core_start=0, core_end=16)
    result = worker.drum_timing_summary(features, duration=16, source_hash="known-audio")
    assert result["sourceArtifactSha256"] == "known-audio"
    assert result["source"]["kind"] == "isolated_percussion"
    assert result["pulse"]["bpm"] == pytest.approx(120, abs=3)
    assert len(result["beatCandidatesSeconds"]) >= 24
    assert result["onsetTimesSeconds"][0] == pytest.approx(0.3, abs=0.03)


def test_cli_unknown_audio_emits_single_json():
    output = subprocess.run([sys.executable, str(MODULE_PATH), "analyze", "--audio", "/nonexistent/local-quality.wav"],
                            capture_output=True, text=True, check=False)
    assert output.returncode == 1
    assert len(output.stdout.strip().splitlines()) == 1
    assert json.loads(output.stdout)["available"] is False
    assert "ValueError" in output.stderr


def test_transcribe_does_not_supply_expected_lyrics(monkeypatch, tmp_path):
    class Recognizer:
        @staticmethod
        def transcribe(audio, **arguments):
            assert arguments["initial_prompt"] is None
            assert arguments["condition_on_previous_text"] is False
            assert arguments["task"] == "transcribe"
            assert arguments["language"] is None
            assert arguments["temperature"] == 0
            assert arguments["path_or_hf_repo"] == str(worker.WHISPER_DIRECTORY)
            return {"text": "바람이 불어오는 이 밤", "language": "ko", "segments": [segment()]}
    monkeypatch.setitem(sys.modules, "mlx_whisper", Recognizer())
    result = worker.transcribe(tmp_path / "song.wav")
    assert result["independentVotes"] == 1
    assert not result["expectedLyricsProvided"]
    assert result["reliable"]


def test_direct_worker_detects_reparenting_but_nested_setup_checks_pid():
    calls = []
    check_pid = lambda pid, signal: calls.append((pid, signal))
    assert not worker.initiating_parent_alive(42, direct_parent=True, getppid=lambda: 1, check_pid=check_pid)
    assert calls == []
    assert worker.initiating_parent_alive(42, direct_parent=False, getppid=lambda: 1, check_pid=check_pid)
    assert calls == [(42, 0)]


def test_watchdog_pid_check_distinguishes_missing_from_existing_process():
    def missing(_pid, _signal):
        raise ProcessLookupError
    def existing(_pid, _signal):
        raise PermissionError
    assert not worker.initiating_parent_alive(42, direct_parent=False, check_pid=missing)
    assert worker.initiating_parent_alive(42, direct_parent=False, check_pid=existing)


def test_standalone_manual_watchdog_does_not_start_a_monitor():
    with worker.parent_watchdog("analyze", environ={}, parent_check=lambda *_args, **_kwargs: pytest.fail("manual run watched a parent")):
        pass


def test_missing_parent_prevents_work_before_entering_guard_body():
    exited = []
    entered = False
    with pytest.raises(RuntimeError, match="exited"):
        with worker.parent_watchdog("analyze", environ={worker.PARENT_PID_ENV: "42"},
                                    parent_check=lambda _pid, **_options: False, on_exit=exited.append):
            entered = True
    assert exited == [125]
    assert not entered


def test_watchdog_stops_running_work_when_parent_disappears():
    alive = threading.Event()
    alive.set()
    exited = threading.Event()
    codes = []
    def stop(code):
        codes.append(code)
        exited.set()
    with worker.parent_watchdog("setup", environ={worker.PARENT_PID_ENV: "42"},
                                parent_check=lambda _pid, **_options: alive.is_set(), on_exit=stop, interval=0.01):
        alive.clear()
        assert exited.wait(1.0)
    assert codes == [125]


def test_analysis_lock_waits_before_any_model_work_and_keeps_inode(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "RUNTIME", tmp_path / "quality")
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"fixture")
    started = threading.Event()
    loaded = threading.Event()
    result = []
    def heavy(_audio):
        loaded.set()
        return {"available": True}
    monkeypatch.setattr(worker, "_analyze_unlocked", heavy)
    def second_worker():
        started.set()
        result.append(worker.analyze(audio))
    with worker.analysis_lock():
        inode = (worker.RUNTIME / "analysis.lock").stat().st_ino
        second = threading.Thread(target=second_worker)
        second.start()
        assert started.wait(1.0)
        assert not loaded.wait(0.05)
    second.join(timeout=1.0)
    assert not second.is_alive()
    assert loaded.is_set()
    assert result == [{"available": True}]
    with worker.analysis_lock():
        assert (worker.RUNTIME / "analysis.lock").stat().st_ino == inode


def test_measure_preserves_model_free_path_and_single_json(tmp_path, monkeypatch, capsys):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(worker, "analysis_lock", lambda: pytest.fail("measure waited for model lock"))
    monkeypatch.setattr(worker, "loudness", lambda _audio: {"status": "measured", "integratedLufs": -14})
    commands = []
    @contextlib.contextmanager
    def watchdog(command):
        commands.append(command)
        yield
    monkeypatch.setattr(worker, "parent_watchdog", watchdog)
    assert worker.main(["measure", "--audio", str(audio)]) == 0
    output = capsys.readouterr()
    assert len(output.out.strip().splitlines()) == 1
    assert json.loads(output.out)["loudness"]["integratedLufs"] == -14
    assert commands == ["measure"]
