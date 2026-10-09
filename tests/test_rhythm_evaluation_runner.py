"""Fresh, immutable evaluation runs keep missing labels, failures and abstentions visible."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from local_music_engine.rhythm_diagnostics import MAX_EVENTS, VERSION as DIAGNOSTICS_VERSION
from local_music_engine.rhythm_evaluation import VERSION
from local_music_engine.storage import sha256_file
from test_signal_diagnostics import RATE, write_audio


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_rhythm_quality.py"
SPEC = importlib.util.spec_from_file_location("rhythm_evaluation_runner_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def audio_case(tmp_path, *, identifier="gap", amplitude=0.2, labelled=True, origin="controlled_injection",
               split="calibration"):
    samples = np.full(8 * RATE, amplitude)
    samples[round(2.7 * RATE):round(3.2 * RATE)] = 0
    audio = write_audio(tmp_path / f"{identifier}.wav", samples)
    return {"caseId": identifier, "audioPath": audio.name, "audioBytes": audio.stat().st_size,
        "sourceArtifactSha256": sha256_file(audio), "durationSeconds": 8,
        "split": split, "labelOrigin": origin, "sourceFamily": identifier,
        "parentArtifactSha256": sha256_file(audio),
        "reviewedRanges": [{"startSeconds": 0, "endSeconds": 8, "categories": ["mix_dropout"]}] if labelled else [],
        "annotations": [{"annotationId": f"{identifier}:gap", "category": "mix_dropout", "label": "defect",
            "startSeconds": 2.7, "endSeconds": 3.2, "evidence": {
                "kind": "controlled_injection" if origin == "controlled_injection" else "human_listening",
                "description": "Known zero-valued PCM intervention", "sourceRef": f"fixture:{identifier}"}}] if labelled else []}


def timing_case(tmp_path, identifier, *, label="defect", split="calibration"):
    audio = write_audio(tmp_path / f"{identifier}.wav", np.full(8 * RATE, 0.1))
    return {"caseId": identifier, "audioPath": str(audio), "audioBytes": audio.stat().st_size,
        "sourceArtifactSha256": sha256_file(audio), "durationSeconds": 8, "split": split,
        "labelOrigin": "controlled_injection", "sourceFamily": "song", "parentArtifactSha256": "a" * 64,
        "reviewedRanges": [{"startSeconds": 2, "endSeconds": 4, "categories": ["beat_timing"]}] if label else [],
        "annotations": [{"annotationId": identifier + ":timing", "category": "beat_timing", "label": label,
            "startSeconds": 2, "endSeconds": 4, "evidence": {"kind": "controlled_injection",
                "description": "Authored time-map support", "sourceRef": "fixture:map"}}] if label else []}


def timing_event(start=2, end=4, *, severity="warning", mode="repetition_displacement"):
    return {"check": "repeated_hit_timing_shift_suspected", "category": "beat_timing", "severity": severity,
        "confidence": 0.7, "startSeconds": start, "endSeconds": end, "retryEligible": False,
        "observed": {"mode": mode, "peakExpectedDisplacementMilliseconds": 190}, "message": "fixture"}


def timing_row(case, events, *, supported=((0, 8),), status="observed"):
    rhythm = {"measuredArtifactSha256": case["sourceArtifactSha256"], "durationSeconds": 8,
        "hitTiming": {"supportedRangesSeconds": [list(item) for item in supported],
                      "supportedFraction": sum(b - a for a, b in supported) / 8},
        "diagnostics": {"version": DIAGNOSTICS_VERSION, "durationSeconds": 8, "status": status,
            "events": events, "findings": events, "checks": {"beatTiming": {"status": "unknown"},
                                                            "hitTiming": {"status": status}},
            "totalEventCount": len(events), "omittedEventCount": 0, "omittedWarningCount": 0,
            "eventsTruncated": False, "maximumEvents": MAX_EVENTS}}
    report = {"sourceArtifactSha256": case["sourceArtifactSha256"], "rhythm": rhythm}
    return {"case": case, "report": report, "score": runner.score_case(case, report)}


def manifest_file(tmp_path, cases):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": VERSION, "cases": cases}), encoding="utf-8")
    return path


def freeze_file(tmp_path, monkeypatch, case, *, calibration=("calibration-song",)):
    """A one-file code tree stands in for the repository."""
    code_root = tmp_path / "code"
    code_root.mkdir()
    code = code_root / "analysis.py"
    code.write_text("frozen analysis\n", encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", code_root)
    monkeypatch.setattr(runner, "FROZEN_CODE", ("analysis.py",))
    source = tmp_path / case["audioPath"]
    path = tmp_path / "freeze.json"
    runner.write_freeze(path, calibration_families=list(calibration), holdout_sources={case["sourceFamily"]: source})
    return path, code


@pytest.mark.parametrize("mutation, message", [
    ("hash", "hash"), ("bytes", "size"), ("duration", "duration"),
])
def test_source_mismatch_is_rejected_before_output_creation_or_any_dsp(tmp_path, monkeypatch, mutation, message):
    case = audio_case(tmp_path)
    if mutation == "hash":
        case["sourceArtifactSha256"] = "b" * 64
    elif mutation == "bytes":
        case["audioBytes"] += 1
    else:
        case["durationSeconds"] = 7.9
        case["reviewedRanges"][0]["endSeconds"] = 7.9
    manifest = manifest_file(tmp_path, [case])
    output = tmp_path / "results"

    def forbidden_analysis(*args, **kwargs):
        raise AssertionError("Verification must finish before DSP")

    monkeypatch.setattr(runner, "inspect_audio_rhythm", forbidden_analysis)
    with pytest.raises(ValueError, match=message):
        runner.evaluate_manifest(manifest, output)
    assert not output.exists()


def test_default_runner_uses_current_diagnostics_fresh_reports_and_preserves_source_and_prior_runs(tmp_path):
    case = audio_case(tmp_path)
    manifest = manifest_file(tmp_path, [case])
    frozen_input = {path: path.read_bytes() for path in (manifest, tmp_path / case["audioPath"])}
    first = runner.evaluate_manifest(manifest, tmp_path / "run-1")
    assert first["complete"] is True
    assert first["diagnosticsVersion"] == DIAGNOSTICS_VERSION == "rhythm-diagnostics-v4"
    assert first["newModelInference"] is False
    assert first["humanListeningRequested"] is False
    assert first["frozenProtocol"] is None
    assert first["scores"][0]["counts"]["truePositives"] == 1
    assert first["scores"][0]["rates"]["recall"] == 1
    first_report = tmp_path / "run-1" / first["reports"][0]["path"]
    first_bytes = first_report.read_bytes()
    report = json.loads(first_bytes)
    assert report["report"]["rhythm"]["diagnostics"]["version"] == DIAGNOSTICS_VERSION
    # The product path measured hit timing itself; no model prediction entered the report.
    assert report["report"]["rhythm"]["hitTiming"]["modelInference"] is False
    assert report["report"]["rhythm"]["hitTiming"]["modelPredictedBeatTimesSeconds"] == []
    assert first["reports"][0]["sha256"] == sha256_file(first_report)
    assert first["reports"][0]["bytes"] == len(first_bytes)
    second = runner.evaluate_manifest(manifest, tmp_path / "run-2")
    assert second["completedCaseCount"] == 1
    assert first_report.read_bytes() == first_bytes
    assert all(path.read_bytes() == frozen for path, frozen in frozen_input.items())
    with pytest.raises(FileExistsError):
        runner.evaluate_manifest(manifest, tmp_path / "run-1")
    assert first_report.read_bytes() == first_bytes


def test_older_diagnostics_can_be_compared_on_the_same_manifest(tmp_path):
    manifest = manifest_file(tmp_path, [audio_case(tmp_path)])
    result = runner.evaluate_manifest(manifest, tmp_path / "v3", diagnostics_version="rhythm-diagnostics-v3")
    report = json.loads((tmp_path / "v3" / result["reports"][0]["path"]).read_text())
    assert report["report"]["rhythm"]["diagnostics"]["version"] == "rhythm-diagnostics-v3"
    assert "hitTiming" not in report["report"]["rhythm"]
    with pytest.raises(ValueError, match="Unsupported"):
        runner.evaluate_manifest(manifest, tmp_path / "future", diagnostics_version="future-unvalidated")


def test_existing_unreviewed_audio_yields_no_natural_accuracy_without_labels(tmp_path):
    case = audio_case(tmp_path, labelled=False, origin="human")
    manifest = manifest_file(tmp_path, [case])
    result = runner.evaluate_manifest(manifest, tmp_path / "unreviewed")
    score, = result["scores"]
    assert score["status"] == "unlabelled"
    assert score["counts"]["unscoredWarningPredictions"] == 1
    assert score["counts"]["falsePositives"] == 0
    assert score["counts"]["truePositives"] == 0
    assert all(value is None for value in score["rates"].values())
    group = result["evaluation"]["groups"]["human:calibration"]
    assert group["unlabelledCaseCount"] == 1
    assert group["rates"]["precision"] is None
    assert group["rates"]["recall"] is None
    coverage = result["coverage"]["groups"]["human:calibration"]
    assert coverage["naturalHumanLabelCount"] == 0
    assert coverage["conservativeRecallIncludingFailuresAndUnscorable"] is None


def test_failed_case_stays_in_conservative_recall_denominator(tmp_path, monkeypatch):
    good = audio_case(tmp_path, identifier="good")
    failed = audio_case(tmp_path, identifier="failed", amplitude=0.25)
    manifest = manifest_file(tmp_path, [good, failed])
    original = runner.inspect_audio_rhythm

    def fail_one(path, **kwargs):
        if Path(path).name == "failed.wav":
            raise ValueError("Deliberately unavailable analysis for coverage regression")
        return original(path, **kwargs)

    monkeypatch.setattr(runner, "inspect_audio_rhythm", fail_one)
    result = runner.evaluate_manifest(manifest, tmp_path / "partial")
    assert result["complete"] is False
    assert result["requestedCaseCount"] == 2
    assert result["completedCaseCount"] == result["failedCaseCount"] == 1
    assert result["failures"][0]["caseId"] == "failed"
    successful = result["evaluation"]["groups"]["controlled_injection:calibration"]
    assert successful["rates"]["recall"] == 1
    coverage = result["coverage"]["groups"]["controlled_injection:calibration"]
    assert coverage["requestedDefectAnnotations"] == 2
    assert coverage["verifiedTruePositives"] == coverage["failedCaseCount"] == 1
    assert coverage["conservativeRecallIncludingFailuresAndUnscorable"] == 0.5
    persisted = json.loads((tmp_path / "partial" / "summary.json").read_text())
    assert persisted["coverage"] == result["coverage"]


def test_related_source_split_leakage_is_rejected_before_writing_reports(tmp_path):
    calibration = audio_case(tmp_path, identifier="calibration")
    holdout = audio_case(tmp_path, identifier="holdout", amplitude=0.25, split="holdout")
    holdout["sourceFamily"] = calibration["sourceFamily"]
    manifest = manifest_file(tmp_path, [calibration, holdout])
    with pytest.raises(ValueError, match="families"):
        runner.evaluate_manifest(manifest, tmp_path / "leaked")
    assert not (tmp_path / "leaked").exists()


def test_runner_cannot_substitute_existing_report_for_a_fresh_measurement(tmp_path):
    case = audio_case(tmp_path)
    case["reportPath"] = "previous.json"
    manifest = manifest_file(tmp_path, [case])
    with pytest.raises(ValueError, match="fresh reports"):
        runner.evaluate_manifest(manifest, tmp_path / "reuse")
    assert not (tmp_path / "reuse").exists()


def test_holdout_requires_a_prior_code_freeze_before_any_dsp(tmp_path, monkeypatch):
    manifest = manifest_file(tmp_path, [audio_case(tmp_path, split="holdout")])
    monkeypatch.setattr(runner, "inspect_audio_rhythm", lambda *args, **kwargs: pytest.fail("DSP ran before freeze"))
    with pytest.raises(ValueError, match="frozen protocol"):
        runner.evaluate_manifest(manifest, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_freeze_records_code_and_untouched_sources_and_is_never_overwritten(tmp_path, monkeypatch):
    case = audio_case(tmp_path, split="holdout")
    frozen, code = freeze_file(tmp_path, monkeypatch, case)
    protocol = json.loads(frozen.read_text())
    assert protocol["version"] == runner.FREEZE_VERSION
    assert protocol["diagnosticsVersion"] == DIAGNOSTICS_VERSION
    assert protocol["codeSha256"] == {"analysis.py": sha256_file(code)}
    assert protocol["holdoutSources"] == [{"sourceFamily": "gap", "sourcePath": str((tmp_path / "gap.wav").resolve()),
        "sourceBytes": (tmp_path / "gap.wav").stat().st_size, "sourceArtifactSha256": case["sourceArtifactSha256"]}]
    with pytest.raises(FileExistsError):
        runner.write_freeze(frozen, calibration_families=["calibration-song"], holdout_sources={"gap": tmp_path / "gap.wav"})
    with pytest.raises(ValueError, match="disjoint"):
        runner.write_freeze(tmp_path / "overlap.json", calibration_families=["gap"], holdout_sources={"gap": tmp_path / "gap.wav"})


def test_frozen_holdout_runs_and_binds_the_protocol_into_the_summary(tmp_path, monkeypatch):
    case = audio_case(tmp_path, split="holdout")
    frozen, _ = freeze_file(tmp_path, monkeypatch, case)
    result = runner.evaluate_manifest(manifest_file(tmp_path, [case]), tmp_path / "run", frozen_protocol=frozen)
    assert result["complete"] is True
    assert result["frozenProtocol"]["protocol"]["sha256"] == sha256_file(frozen)
    assert result["frozenProtocol"]["holdoutSources"][0]["sourceFamily"] == "gap"
    assert result["scores"][0]["counts"]["truePositives"] == 1


def test_code_changed_after_the_freeze_is_rejected_before_measurement(tmp_path, monkeypatch):
    case = audio_case(tmp_path, split="holdout")
    frozen, code = freeze_file(tmp_path, monkeypatch, case)
    code.write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(runner, "inspect_audio_rhythm", lambda *args, **kwargs: pytest.fail("DSP ran on changed code"))
    with pytest.raises(ValueError, match="Frozen code hash"):
        runner.evaluate_manifest(manifest_file(tmp_path, [case]), tmp_path / "run", frozen_protocol=frozen)
    assert not (tmp_path / "run").exists()


def test_code_change_during_measurement_rejects_the_success_summary(tmp_path, monkeypatch):
    case = audio_case(tmp_path, split="holdout")
    frozen, code = freeze_file(tmp_path, monkeypatch, case)
    original = runner.inspect_audio_rhythm

    def tamper(path, **kwargs):
        code.write_text("tampered\n", encoding="utf-8")
        return original(path, **kwargs)

    monkeypatch.setattr(runner, "inspect_audio_rhythm", tamper)
    with pytest.raises(ValueError, match="Evidence file (size|hash) changed"):
        runner.evaluate_manifest(manifest_file(tmp_path, [case]), tmp_path / "run", frozen_protocol=frozen)
    assert not (tmp_path / "run" / "summary.json").exists()


@pytest.mark.parametrize("mutation", ["parent_hash", "family", "diagnostics"])
def test_holdout_manifest_must_match_the_frozen_source_selection(tmp_path, monkeypatch, mutation):
    case = audio_case(tmp_path, split="holdout")
    frozen, _ = freeze_file(tmp_path, monkeypatch, case)
    version = DIAGNOSTICS_VERSION
    if mutation == "parent_hash":
        case["parentArtifactSha256"] = "f" * 64
    elif mutation == "family":
        case["sourceFamily"] = "other-song"
    else:
        version = "rhythm-diagnostics-v3"
    monkeypatch.setattr(runner, "inspect_audio_rhythm", lambda *args, **kwargs: pytest.fail("Holdout ran before validation"))
    with pytest.raises(ValueError, match="frozen selection|does not cover"):
        runner.evaluate_manifest(manifest_file(tmp_path, [case]), tmp_path / "run", frozen_protocol=frozen,
                                 diagnostics_version=version)
    assert not (tmp_path / "run").exists()


def test_warning_already_in_the_original_is_not_a_new_detection(tmp_path):
    original = timing_case(tmp_path, "song-baseline", label=None)
    defect = timing_case(tmp_path, "song-alternating-90ms-4s")
    quiet = timing_case(tmp_path, "song-burst-90ms-4s")
    rows = [timing_row(original, [timing_event()]), timing_row(defect, [timing_event()]), timing_row(quiet, [])]
    audit = runner.timing_audit({"cases": [original, defect, quiet]}, rows, [])
    group = audit["groups"]["controlled_injection:calibration:controlled_timing_defect"]
    assert group["intervalCount"] == 2
    assert group["localizedWarningIntervals"] == 1
    assert group["newLocalizedWarningIntervals"] == 0
    assert group["noWarningWithSupportIntervals"] == 1
    assert audit["groups"]["controlled_injection:calibration:unlabelled_baseline"]["unlabelledWarningCount"] == 1
    assert audit["intervals"][0]["warningAlsoInOriginal"] is True


def test_no_warning_without_support_is_an_abstention_not_a_correct_gradual_judgement(tmp_path):
    unsupported = timing_case(tmp_path, "song-smooth-tempo-no-plan", label="intentional")
    supported = timing_case(tmp_path, "other-smooth-tempo-with-plan", label="intentional")
    rows = [timing_row(unsupported, [], supported=(), status="unknown"),
            timing_row(supported, [timing_event(2, 4, severity="info", mode="tempo_drift")], supported=((1, 3),))]
    audit = runner.timing_audit({"cases": [unsupported, supported]}, rows, [])
    blind = audit["groups"]["controlled_injection:calibration:smooth_tempo_no_plan"]
    assert blind["noSupportIntervals"] == blind["noWarningWithoutSupportIntervals"] == 1
    assert blind["noWarningWithSupportIntervals"] == blind["gradualChangeObservedIntervals"] == 0
    seen = audit["groups"]["controlled_injection:calibration:smooth_tempo_with_plan"]
    assert seen["partialSupportIntervals"] == seen["noWarningWithSupportIntervals"] == 1
    assert seen["gradualChangeObservedIntervals"] == 1
    assert audit["intervals"][1]["supportedFraction"] == 0.5
    assert audit["intervals"][1]["gradualChangePeakMilliseconds"] == 190
    assert "coverage, not accuracy" in audit["interpretation"]


def test_broad_warning_over_a_gradual_change_stays_visible_as_a_false_alert(tmp_path):
    gradual = timing_case(tmp_path, "song-smooth-tempo-no-plan", label="intentional")
    row = timing_row(gradual, [timing_event(0.5, 7.5)])
    assert row["score"]["counts"]["intentionalFalseAlerts"] == 0  # Outside the reviewed range for the scorer.
    audit = runner.timing_audit({"cases": [gradual]}, [row], [])
    group = audit["groups"]["controlled_injection:calibration:smooth_tempo_no_plan"]
    assert group["anyWarningIntervals"] == group["halfCoveringWarningIntervals"] == 1
    assert group["localizedWarningIntervals"] == 0
