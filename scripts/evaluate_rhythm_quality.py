#!/usr/bin/env python3
"""Evaluate existing, hash-bound music and explicit temporal labels without models.

The output directory must be new. No project, source audio, stored inspection or
human review is changed. Controlled interventions and natural human labels stay
separate, including when no natural labels are available. Holdout cases need a
code freeze written before their audio existed; detection, measurement support
and abstention are reported side by side.

uv run python scripts/evaluate_rhythm_quality.py --freeze .runtime/new/freeze.json \
    --calibration-family song-a --holdout-source song-b=/absolute/song-b.wav
uv run python scripts/evaluate_rhythm_quality.py --manifest .runtime/new/corpus.json \
    --frozen-protocol .runtime/new/freeze.json --output-dir .runtime/new/run
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_music_engine.rhythm_diagnostics import SUPPORTED_VERSIONS, VERSION as DIAGNOSTICS_VERSION
from local_music_engine.rhythm_evaluation import aggregate_scores, score_case, validate_manifest
from local_music_engine.rhythm_evidence import load_cached_stem_evidence
from local_music_engine.rhythm_inspection import inspect_audio_rhythm
from local_music_engine.rhythm_intent import load_rhythm_intent
from local_music_engine.storage import atomic_write_json, sha256_file, utc_now

FREEZE_VERSION = "rhythm-evaluation-frozen-protocol-v1"
# Everything that decides a measurement or a score. Changing any of these
# after the freeze invalidates a holdout run.
FROZEN_CODE = (
    "scripts/evaluate_rhythm_quality.py", "scripts/build_rhythm_evaluation.py",
    "src/local_music_engine/timbre_tracking.py", "src/local_music_engine/rhythm_inspection.py",
    "src/local_music_engine/rhythm_diagnostics.py", "src/local_music_engine/rhythm_evaluation.py",
    "src/local_music_engine/percussion_analysis.py", "src/local_music_engine/music_structure.py",
    "src/local_music_engine/rhythm_intent.py", "src/local_music_engine/signal_diagnostics.py",
    "src/local_music_engine/rhythm_context.py", "src/local_music_engine/rhythm_evidence.py",
    "src/local_music_engine/storage.py",
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FAMILY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Evaluation JSON must not contain duplicate keys")
        result[key] = value
    return result


def _read_json(path: Path, limit: int) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"Evaluation JSON is too large: {path}")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("Evaluation JSON must be an object")
    return value


def read_manifest(path: Path) -> dict:
    manifest = validate_manifest(_read_json(path, 4_000_000))
    for case in manifest["cases"]:
        if "audioPath" not in case:
            raise ValueError("Live evaluation requires an explicit audioPath for every case")
        if "reportPath" in case:
            raise ValueError("Live evaluation creates fresh reports rather than reusing reportPath")
        for key in ("audioPath", "intentPath", "stemCachePath", "evidenceAudioPath"):
            if key in case:
                supplied = Path(case[key]).expanduser()
                case[key] = str((supplied if supplied.is_absolute() else path.parent / supplied).resolve())
        if "stemCachePath" not in case and any(key in case for key in ("stemLabel", "evidenceAudioPath")):
            raise ValueError("Stem label/reference requires an explicit cache")
    return manifest


def _verify_audio(case: dict) -> None:
    path = Path(case["audioPath"])
    if "audioBytes" in case and path.stat().st_size != case["audioBytes"]:
        raise ValueError("Evaluation source audio size differs from the frozen manifest")
    if sha256_file(path) != case["sourceArtifactSha256"]:
        raise ValueError("Evaluation source audio hash differs from the frozen manifest")
    with wave.open(str(path), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
    if not math.isfinite(duration) or abs(duration - case["durationSeconds"]) > 0.00011:
        raise ValueError("Evaluation source audio duration differs from the frozen manifest")


def _record(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Evidence file is missing: {path}")
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _verify_record(record: dict) -> None:
    path = Path(record["path"])
    if not path.is_file() or path.stat().st_size != record["bytes"]:
        raise ValueError(f"Evidence file size changed: {path}")
    if sha256_file(path) != record["sha256"]:
        raise ValueError(f"Evidence file hash changed: {path}")


def write_freeze(path: Path, *, calibration_families: list[str], holdout_sources: dict[str, Path],
                 diagnostics_version: str = DIAGNOSTICS_VERSION) -> dict:
    """Record the code and the still-untouched holdout sources before any holdout audio is made."""
    path = path.expanduser().resolve()
    if path.exists():
        raise FileExistsError(path)
    families = [*calibration_families, *holdout_sources]
    if (not calibration_families or not holdout_sources or len(set(families)) != len(families)
            or any(_FAMILY.fullmatch(family) is None for family in families)):
        raise ValueError("A freeze needs unique, disjoint calibration and holdout family names")
    if diagnostics_version not in SUPPORTED_VERSIONS:
        raise ValueError("Unsupported rhythm diagnostics version")
    sources = []
    for family, source in holdout_sources.items():
        record = _record(Path(source).expanduser().resolve())
        sources.append({"sourceFamily": family, "sourcePath": record["path"],
                        "sourceBytes": record["bytes"], "sourceArtifactSha256": record["sha256"]})
    protocol = {"version": FREEZE_VERSION, "createdAt": utc_now(), "diagnosticsVersion": diagnostics_version,
                "codeSha256": {relative: sha256_file(ROOT / relative) for relative in FROZEN_CODE},
                "calibrationFamilies": list(calibration_families), "holdoutSources": sources}
    atomic_write_json(path, protocol)
    return protocol


def _frozen(protocol_path: Path | None, manifest: dict, diagnostics_version: str) -> dict | None:
    """Holdout audio may only be measured by the code that existed before it did."""
    holdout = [case for case in manifest["cases"] if case["split"] == "holdout"]
    if protocol_path is None:
        if holdout:
            raise ValueError("Holdout evaluation requires a pre-existing frozen protocol")
        return None
    protocol_path = protocol_path.expanduser().resolve()
    protocol_record = _record(protocol_path)
    protocol = _read_json(protocol_path, 1_000_000)
    code, families, sources = protocol.get("codeSha256"), protocol.get("calibrationFamilies"), protocol.get("holdoutSources")
    if protocol.get("version") != FREEZE_VERSION or protocol.get("diagnosticsVersion") != diagnostics_version:
        raise ValueError("Frozen protocol does not cover this evaluation and diagnostics version")
    if not isinstance(code, dict) or set(code) != set(FROZEN_CODE):
        raise ValueError("Frozen protocol must bind every analysis and scoring code dependency")
    if (not isinstance(families, list) or not families or not isinstance(sources, list) or not sources
            or any(not isinstance(family, str) for family in families)):
        raise ValueError("Frozen protocol requires explicit calibration families and holdout sources")
    code_records = []
    for relative in FROZEN_CODE:
        record = _record(ROOT / relative)
        if record["sha256"] != code[relative]:
            raise ValueError(f"Frozen code hash differs: {relative}")
        code_records.append(record)
    selected, source_records = {}, []
    for item in sources:
        if (not isinstance(item, dict) or set(item) != {"sourceFamily", "sourcePath", "sourceBytes", "sourceArtifactSha256"}
                or not isinstance(item["sourceFamily"], str) or not isinstance(item["sourcePath"], str)
                or type(item["sourceBytes"]) is not int or not isinstance(item["sourceArtifactSha256"], str)
                or _SHA256.fullmatch(item["sourceArtifactSha256"]) is None):
            raise ValueError("Frozen holdout source requires family, path, byte count and SHA-256")
        if item["sourceFamily"] in families or item["sourceFamily"] in selected:
            raise ValueError("Frozen holdout source family overlaps calibration or is listed twice")
        record = {"path": item["sourcePath"], "bytes": item["sourceBytes"], "sha256": item["sourceArtifactSha256"]}
        _verify_record(record)
        selected[item["sourceFamily"]] = item["sourceArtifactSha256"]
        source_records.append(record)
    for case in manifest["cases"]:
        family, parent = case.get("sourceFamily"), case.get("parentArtifactSha256")
        if case["split"] == "holdout":
            if selected.get(family) != parent:
                raise ValueError("Holdout manifest source family or parent hash differs from the frozen selection")
        elif family not in families or parent in selected.values():
            raise ValueError("Calibration manifest source differs from the frozen calibration families")
    return {"protocol": protocol_record, "code": code_records, "sources": source_records,
            "calibrationFamilies": list(families), "holdoutSources": sources}


def _verify_freeze(freeze: dict | None) -> None:
    if freeze is not None:
        for record in (freeze["protocol"], *freeze["code"], *freeze["sources"]):
            _verify_record(record)


def coverage_summary(manifest: dict, scores: list[dict], failures: list[dict]) -> dict:
    """Expose failures/unscorable cases in the requested-positive denominator."""
    scored = {score["caseId"]: score for score in scores}
    failed = {item["caseId"] for item in failures}
    groups = {}
    for case in manifest["cases"]:
        key = f"{case['labelOrigin']}:{case['split']}"
        group = groups.setdefault(key, {"requestedCaseCount": 0, "failedCaseCount": 0,
            "unscorableCaseCount": 0, "unexecutedCaseCount": 0, "requestedDefectAnnotations": 0,
            "verifiedTruePositives": 0, "naturalHumanLabelCount": 0})
        group["requestedCaseCount"] += 1
        group["requestedDefectAnnotations"] += sum(annotation["label"] == "defect" for annotation in case["annotations"])
        group["naturalHumanLabelCount"] += len(case["annotations"]) if case["labelOrigin"] == "human" else 0
        result = scored.get(case["caseId"])
        if case["caseId"] in failed:
            group["failedCaseCount"] += 1
        elif result is None:
            group["unexecutedCaseCount"] += 1
        elif result["status"] == "unscorable":
            group["unscorableCaseCount"] += 1
        elif result["status"] == "scored":
            group["verifiedTruePositives"] += result["counts"]["truePositives"]
    for group in groups.values():
        denominator = group["requestedDefectAnnotations"]
        group["conservativeRecallIncludingFailuresAndUnscorable"] = group["verifiedTruePositives"] / denominator if denominator else None
    return {"groups": groups, "interpretation": "Only validated localized positives count. Failed, unexecuted and unscorable cases remain in the requested defect denominator; absence of human labels gives no natural accuracy."}


def _events(report: dict, severity: tuple[str, ...]) -> list[dict]:
    return [event for event in report["rhythm"]["diagnostics"]["events"]
            if event["category"] == "beat_timing" and event["severity"] in severity]


def _overlap(start: float, end: float, other_start: float, other_end: float) -> float:
    return max(0.0, min(end, other_end) - max(start, other_start))


def _localized(annotation: dict, event: dict) -> bool:
    shared = _overlap(annotation["startSeconds"], annotation["endSeconds"], event["startSeconds"], event["endSeconds"])
    truth = annotation["endSeconds"] - annotation["startSeconds"]
    prediction = event["endSeconds"] - event["startSeconds"]
    return shared / truth >= 0.5 and shared / prediction >= 0.5 and shared / (truth + prediction - shared) >= 0.2


def _condition(case: dict) -> str:
    identifier = case["caseId"]
    if "smooth-tempo" in identifier:
        return "smooth_tempo_with_plan" if "with-plan" in identifier else "smooth_tempo_no_plan"
    if identifier.endswith("-baseline"):
        return "unlabelled_baseline"
    if "uniform-gain" in identifier:
        return "unlabelled_uniform_gain"
    if "smooth-fade" in identifier:
        return "unlabelled_smooth_fade"
    if any(annotation["category"] == "beat_timing" and annotation["label"] == "defect" for annotation in case["annotations"]):
        return "controlled_timing_defect"
    return "digital_mix_gap" if "mix-gap" in identifier else "other"


def timing_audit(manifest: dict, rows: list[dict], failures: list[dict]) -> dict:
    """Detection, false alerts and abstention for every known timing interval.

    An interval without measurement support is an abstention: it is neither a
    detection nor evidence that a gradual change was correctly left alone. A
    warning also present in the untouched original is not a new detection.
    """
    by_case = {row["case"]["caseId"]: row for row in rows}
    failed = {failure["caseId"] for failure in failures}
    originals = {(row["case"].get("sourceFamily"), row["case"].get("parentArtifactSha256")): row
                 for row in rows if row["case"]["caseId"].endswith("-baseline")}
    groups, intervals, unlabelled = {}, [], []
    for case in manifest["cases"]:
        condition = _condition(case)
        key = f"{case['labelOrigin']}:{case['split']}:{condition}"
        group = groups.setdefault(key, {"requestedCaseCount": 0, "completedCaseCount": 0, "failedCaseCount": 0,
            "intervalCount": 0, "fullSupportIntervals": 0, "partialSupportIntervals": 0, "noSupportIntervals": 0,
            "localizedWarningIntervals": 0, "newLocalizedWarningIntervals": 0, "anyWarningIntervals": 0,
            "halfCoveringWarningIntervals": 0, "noWarningWithSupportIntervals": 0,
            "noWarningWithoutSupportIntervals": 0, "gradualChangeObservedIntervals": 0,
            "unlabelledWarningCount": 0, "uniqueWaveforms": set()})
        group["requestedCaseCount"] += 1
        group["uniqueWaveforms"].add(case["sourceArtifactSha256"])
        row = by_case.get(case["caseId"])
        if row is None:
            group["failedCaseCount"] += case["caseId"] in failed
            continue
        group["completedCaseCount"] += 1
        timing = row["report"]["rhythm"].get("hitTiming") or {}
        supported = timing.get("supportedRangesSeconds", [])
        warnings, observations = _events(row["report"], ("warning", "error")), _events(row["report"], ("info",))
        labelled = [annotation for annotation in case["annotations"]
                    if annotation["category"] == "beat_timing" and annotation["label"] != "uncertain"]
        if not labelled and condition.startswith("unlabelled"):
            group["unlabelledWarningCount"] += len(warnings)
            unlabelled.append({"caseId": case["caseId"], "warningRangesSeconds":
                               [[event["startSeconds"], event["endSeconds"]] for event in warnings],
                               "supportedFraction": timing.get("supportedFraction")})
        original = originals.get((case.get("sourceFamily"), case.get("parentArtifactSha256")))
        for annotation in labelled:
            start, end = annotation["startSeconds"], annotation["endSeconds"]
            covered = sum(_overlap(start, end, left, right) for left, right in supported) / (end - start)
            localized = [event for event in warnings if _localized(annotation, event)]
            touching = [event for event in warnings if _overlap(start, end, event["startSeconds"], event["endSeconds"]) > 0]
            half = [event for event in touching
                    if _overlap(start, end, event["startSeconds"], event["endSeconds"]) / (end - start) >= 0.5]
            already = bool(original) and any(_localized(annotation, event)
                                             for event in _events(original["report"], ("warning", "error")))
            gradual = [event for event in observations if event.get("observed", {}).get("mode") == "tempo_drift"
                       and _overlap(start, end, event["startSeconds"], event["endSeconds"]) / (end - start) >= 0.5]
            group["intervalCount"] += 1
            group["fullSupportIntervals" if covered >= 0.999 else "partialSupportIntervals" if covered > 0.001
                  else "noSupportIntervals"] += 1
            group["localizedWarningIntervals"] += bool(localized)
            group["newLocalizedWarningIntervals"] += bool(localized) and not already
            group["anyWarningIntervals"] += bool(touching)
            group["halfCoveringWarningIntervals"] += bool(half)
            group["noWarningWithSupportIntervals"] += not touching and covered > 0.001
            group["noWarningWithoutSupportIntervals"] += not touching and covered <= 0.001
            group["gradualChangeObservedIntervals"] += bool(gradual)
            intervals.append({"caseId": case["caseId"], "annotationId": annotation["annotationId"],
                "label": annotation["label"], "condition": condition, "startSeconds": start, "endSeconds": end,
                "supportedFraction": round(covered, 4), "localizedWarning": bool(localized),
                "warningAlsoInOriginal": already, "warningRangesSeconds":
                    [[event["startSeconds"], event["endSeconds"]] for event in touching],
                "gradualChangePeakMilliseconds": max((event["observed"].get("peakExpectedDisplacementMilliseconds", 0)
                                                      for event in gradual), default=None)})
    for group in groups.values():
        group["uniqueWaveforms"] = len(group["uniqueWaveforms"])
    return {"groups": {key: groups[key] for key in sorted(groups)}, "intervals": intervals, "unlabelled": unlabelled,
            "interpretation": "Support is the share of a known interval where hits were measured against repetitions; it is coverage, not accuracy. No warning without support is an abstention. No-plan and with-plan gradual cases share one WAV and are not independent samples. Warnings on unlabelled originals are observations without truth."}


def evaluate_manifest(manifest_path: Path, output_dir: Path, *, diagnostics_version: str = DIAGNOSTICS_VERSION,
                      frozen_protocol: Path | None = None) -> dict:
    manifest_path = manifest_path.expanduser().resolve()
    manifest_record = _record(manifest_path)
    manifest = read_manifest(manifest_path)
    if diagnostics_version not in SUPPORTED_VERSIONS:
        raise ValueError("Unsupported rhythm diagnostics version")
    freeze = _frozen(frozen_protocol, manifest, diagnostics_version)
    # Validate all source assets before creating a run or executing any DSP.
    for case in manifest["cases"]:
        _verify_audio(case)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    records, scores, failures, rows = [], [], [], []
    for index, case in enumerate(manifest["cases"]):
        try:
            _verify_freeze(freeze)
            _verify_audio(case)
            intent = load_rhythm_intent(case["intentPath"], audio=case["audioPath"]) if "intentPath" in case else None
            stems = load_cached_stem_evidence(case["stemCachePath"], audio=case["audioPath"],
                label=case.get("stemLabel"), evidence_audio=case.get("evidenceAudioPath")) if "stemCachePath" in case else None
            rhythm = inspect_audio_rhythm(case["audioPath"], stem_inputs=stems,
                                         intent_snapshot=intent, diagnostics_version=diagnostics_version)
            _verify_audio(case)
            report = {"caseId": case["caseId"], "sourceArtifactSha256": case["sourceArtifactSha256"],
                      "createdAt": utc_now(), "newModelInference": False, "rhythm": rhythm}
            score = score_case(case, report)
            destination = output_dir / f"{index:04d}.json"
            atomic_write_json(destination, {"case": case, "report": report, "score": score})
            rows.append({"case": case, "report": report, "score": score})
            records.append({"caseId": case["caseId"], "path": destination.name,
                "sha256": sha256_file(destination), "bytes": destination.stat().st_size,
                "scoreStatus": score["status"], "warningCount": sum(event["severity"] == "warning" for event in rhythm["diagnostics"]["events"])})
            scores.append(score)
            print(json.dumps({"caseId": case["caseId"], "scoreStatus": score["status"],
                              "counts": score["counts"], "rates": score["rates"]}), flush=True)
        except (Exception, KeyboardInterrupt) as error:
            failures.append({"caseId": case["caseId"], "error": f"{type(error).__name__}: {error}"})
            print(json.dumps({"caseId": case["caseId"], "failed": True, "error": str(error)}), flush=True)
            if isinstance(error, KeyboardInterrupt):
                break
    # A change to the manifest, the code or a frozen source voids the whole run.
    _verify_record(manifest_record)
    _verify_freeze(freeze)
    summary = {"version": "rhythm-evaluation-run-v2", "createdAt": utc_now(),
        "manifest": manifest_record, "manifestSha256": manifest_record["sha256"],
        "diagnosticsVersion": diagnostics_version, "frozenProtocol": freeze,
        "newModelInference": False, "humanListeningRequested": False, "complete": not failures,
        "requestedCaseCount": len(manifest["cases"]), "completedCaseCount": len(scores),
        "failedCaseCount": len(failures), "failures": failures, "reports": records,
        "scores": scores, "evaluation": aggregate_scores(scores),
        "coverage": coverage_summary(manifest, scores, failures),
        "timingAudit": timing_audit(manifest, rows, failures),
        "limitations": [
            "Controlled edits on real music test the imposed signal changes, not natural model-error accuracy.",
            "Original unreviewed music has no implicit clean or intentional-expression labels.",
            "Failures and unscorable/truncated cases are reported and cannot establish successful detection.",
            "Source families must remain disjoint between calibration and holdout; calibration results are not holdout performance.",
        ]}
    atomic_write_json(output_dir / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--diagnostics-version", choices=SUPPORTED_VERSIONS, default=DIAGNOSTICS_VERSION)
    parser.add_argument("--frozen-protocol", type=Path, help="existing freeze; required for holdout cases")
    parser.add_argument("--freeze", type=Path, help="write a new freeze of the current code instead of evaluating")
    parser.add_argument("--calibration-family", action="append", default=[])
    parser.add_argument("--holdout-source", action="append", default=[], metavar="FAMILY=WAV")
    args = parser.parse_args()
    if args.freeze is not None:
        if args.manifest or args.output_dir or args.frozen_protocol:
            parser.error("--freeze only records code and sources; evaluate in a separate run")
        sources = dict(item.split("=", 1) for item in args.holdout_source if "=" in item)
        if len(sources) != len(args.holdout_source):
            parser.error("--holdout-source needs FAMILY=WAV")
        protocol = write_freeze(args.freeze, calibration_families=args.calibration_family,
                                holdout_sources={family: Path(source) for family, source in sources.items()},
                                diagnostics_version=args.diagnostics_version)
        print(json.dumps({"freeze": str(args.freeze), "codeFiles": len(protocol["codeSha256"]),
                          "holdoutFamilies": [source["sourceFamily"] for source in protocol["holdoutSources"]]}), flush=True)
        return 0
    if args.manifest is None or args.output_dir is None:
        parser.error("Evaluation requires --manifest and --output-dir")
    report = evaluate_manifest(args.manifest, args.output_dir, diagnostics_version=args.diagnostics_version,
                               frozen_protocol=args.frozen_protocol)
    print(json.dumps({"completedCaseCount": report["completedCaseCount"], "failedCaseCount": report["failedCaseCount"],
                      "evaluation": report["evaluation"], "timingAudit": report["timingAudit"]["groups"]}), flush=True)
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
