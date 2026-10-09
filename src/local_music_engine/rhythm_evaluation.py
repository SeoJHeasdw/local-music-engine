"""Empirical temporal scoring from explicit, source-bound listening labels.

Controlled injections measure only those interventions. Human listening labels
remain a separate population. No labels, unreviewed audio and uncertain labels
cannot establish natural-audio accuracy. Detection confidence is never consumed
as a calibrated probability of a defect.
"""

from __future__ import annotations

import math
import re
from collections import deque
from typing import Any

from .rhythm_diagnostics import SUPPORTED_VERSIONS

VERSION = "rhythm-evaluation-v1"
CATEGORIES = ("beat_timing", "backing_dropout", "mix_dropout")
MAX_CASES = 10_000
MAX_ANNOTATIONS = 2048
MAX_PREDICTIONS = 10_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COUNTS = ("defectAnnotations", "intentionalAnnotations", "uncertainAnnotations", "reviewedWarningPredictions",
           "truePositives", "falsePositives", "falseNegatives", "intentionalFalseAlerts",
           "intentionalCompatibleObservations", "unscoredPredictions", "unscoredWarningPredictions",
           "unknownOrAbstainedDefects", "undetectedDefects")
_MATCH_POLICY = {"minimumIoU": 0.2, "minimumAnnotationCoverage": 0.5, "minimumPredictionCoverage": 0.5,
                 "matching": "deterministic_maximum_cardinality_one_to_one",
                 "intentionalAlertCoverage": 0.5}


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _text(value: Any, name: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be nonempty text of at most {maximum} characters")
    return value


def _interval(item: dict[str, Any], duration: float) -> tuple[float, float]:
    start = _number(item.get("startSeconds"), "startSeconds")
    end = _number(item.get("endSeconds"), "endSeconds")
    if not 0 <= start < end <= duration:
        raise ValueError("Evaluation interval must fall within the source audio duration")
    return start, end


def _intersection(left: dict[str, Any], right: dict[str, Any]) -> float:
    return max(0.0, min(left["endSeconds"], right["endSeconds"]) - max(left["startSeconds"], right["startSeconds"]))


def _reviewed(item: dict[str, Any], ranges: list[dict[str, Any]], category: str) -> bool:
    stop = item["endSeconds"]
    cursor = item["startSeconds"]
    for region in sorted(ranges, key=lambda value: (value["startSeconds"], value["endSeconds"])):
        if category not in region["categories"] or region["endSeconds"] <= cursor:
            continue
        if region["startSeconds"] > cursor + 1e-8:
            return False
        cursor = max(cursor, region["endSeconds"])
        if cursor >= stop - 1e-8:
            return True
    return False


def validate_case(case: Any) -> dict[str, Any]:
    """Validate truth provenance and coverage, never infer labels from a report."""
    required = {"caseId", "split", "labelOrigin", "sourceArtifactSha256", "durationSeconds", "reviewedRanges", "annotations"}
    optional = {"audioPath", "intentPath", "stemCachePath", "reportPath", "stemLabel", "evidenceAudioPath",
                "parentArtifactSha256", "sourceFamily", "audioBytes"}
    if not isinstance(case, dict) or not required <= set(case) or set(case) - required - optional:
        raise ValueError("Evaluation case has missing or unsupported fields")
    normalized = {"caseId": _text(case["caseId"], "caseId"), "split": case["split"],
                  "labelOrigin": case["labelOrigin"], "sourceArtifactSha256": case["sourceArtifactSha256"]}
    if normalized["split"] not in ("calibration", "holdout"):
        raise ValueError("Evaluation split must be calibration or holdout")
    if normalized["labelOrigin"] not in ("human", "controlled_injection"):
        raise ValueError("Evaluation labelOrigin must be human or controlled_injection")
    if not isinstance(normalized["sourceArtifactSha256"], str) or not _SHA256.fullmatch(normalized["sourceArtifactSha256"]):
        raise ValueError("Evaluation source requires a lowercase SHA-256 hash")
    duration = _number(case["durationSeconds"], "durationSeconds")
    if not 0 < duration <= 610:
        raise ValueError("Evaluation duration must be within (0, 610] seconds")
    normalized["durationSeconds"] = duration
    for key in optional & set(case):
        if key == "audioBytes":
            normalized[key] = _integer(case[key], key)
            if normalized[key] == 0:
                raise ValueError("Evaluation audioBytes must be positive")
        elif key == "parentArtifactSha256":
            if not isinstance(case[key], str) or not _SHA256.fullmatch(case[key]):
                raise ValueError("Evaluation parent artifact requires a lowercase SHA-256 hash")
            normalized[key] = case[key]
        else:
            normalized[key] = _text(case[key], key, 4096)
    regions = case["reviewedRanges"]
    annotations = case["annotations"]
    if not isinstance(regions, list) or len(regions) > MAX_ANNOTATIONS or not isinstance(annotations, list) or len(annotations) > MAX_ANNOTATIONS:
        raise ValueError("Evaluation reviewed ranges and annotations must be bounded lists")
    normalized["reviewedRanges"] = []
    for region in regions:
        if not isinstance(region, dict) or set(region) != {"startSeconds", "endSeconds", "categories"}:
            raise ValueError("Evaluation reviewed range has missing or unsupported fields")
        start, end = _interval(region, duration)
        categories = region["categories"]
        if not isinstance(categories, list) or not categories or any(value not in CATEGORIES for value in categories) or len(set(categories)) != len(categories):
            raise ValueError("Reviewed categories must be unique supported categories")
        normalized["reviewedRanges"].append({"startSeconds": start, "endSeconds": end, "categories": list(categories)})
    normalized["annotations"] = []
    identifiers: set[str] = set()
    for annotation in annotations:
        fields = {"annotationId", "category", "label", "startSeconds", "endSeconds", "evidence"}
        if not isinstance(annotation, dict) or set(annotation) != fields:
            raise ValueError("Evaluation annotation has missing or unsupported fields")
        identifier = _text(annotation["annotationId"], "annotationId")
        if identifier in identifiers:
            raise ValueError("Evaluation annotation IDs must be unique")
        identifiers.add(identifier)
        category, label = annotation["category"], annotation["label"]
        if category not in CATEGORIES or label not in ("defect", "intentional", "uncertain"):
            raise ValueError("Evaluation annotation category or label is unsupported")
        start, end = _interval(annotation, duration)
        evidence = annotation["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != {"kind", "description", "sourceRef"}:
            raise ValueError("Evaluation annotation requires explicit evidence provenance")
        required_kind = "human_listening" if normalized["labelOrigin"] == "human" else "controlled_injection"
        if evidence["kind"] != required_kind:
            raise ValueError("Annotation evidence kind does not match labelOrigin")
        item = {"annotationId": identifier, "category": category, "label": label,
                "startSeconds": start, "endSeconds": end, "evidence": {
                    "kind": required_kind, "description": _text(evidence["description"], "evidence description", 2048),
                    "sourceRef": _text(evidence["sourceRef"], "evidence sourceRef", 4096)}}
        if not _reviewed(item, normalized["reviewedRanges"], category):
            raise ValueError("Every annotation must be inside category-specific reviewed coverage")
        if any(previous["category"] == category and _intersection(item, previous) > 0 for previous in normalized["annotations"]):
            raise ValueError("Same-category truth annotations must not overlap")
        normalized["annotations"].append(item)
    normalized["annotations"].sort(key=lambda item: (item["category"], item["startSeconds"], item["annotationId"]))
    return normalized


def validate_manifest(manifest: Any) -> dict[str, Any]:
    """A manifest contains only explicit cases; paths are read by the caller."""
    if not isinstance(manifest, dict) or set(manifest) != {"version", "cases"} or manifest["version"] != VERSION:
        raise ValueError("Unsupported rhythm evaluation manifest")
    if not isinstance(manifest["cases"], list) or len(manifest["cases"]) > MAX_CASES:
        raise ValueError("Evaluation manifest cases must be a bounded list")
    cases = [validate_case(case) for case in manifest["cases"]]
    if len({case["caseId"] for case in cases}) != len(cases):
        raise ValueError("Evaluation case IDs must be unique")
    if len({(case["sourceArtifactSha256"], case["split"]) for case in cases}) != len({case["sourceArtifactSha256"] for case in cases}):
        raise ValueError("The same source audio must not cross calibration and holdout splits")
    for key in ("parentArtifactSha256", "sourceFamily"):
        labelled = [case for case in cases if key in case]
        if len({(case[key], case["split"]) for case in labelled}) != len({case[key] for case in labelled}):
            raise ValueError("Related source families must not cross calibration and holdout splits")
    return {"version": VERSION, "cases": cases}


def _prediction_data(case: dict[str, Any], report: Any) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    if not isinstance(report, dict) or report.get("sourceArtifactSha256") != case["sourceArtifactSha256"]:
        raise ValueError("Evaluation report source hash does not match the labelled audio")
    rhythm = report.get("rhythm")
    diagnosis = rhythm.get("diagnostics") if isinstance(rhythm, dict) else report.get("diagnostics")
    if not isinstance(diagnosis, dict):
        raise ValueError("Evaluation report requires rhythm diagnostics")
    if diagnosis.get("version") not in SUPPORTED_VERSIONS:
        raise ValueError("Evaluation report has an unsupported diagnostics version")
    if isinstance(rhythm, dict) and rhythm.get("measuredArtifactSha256") != case["sourceArtifactSha256"]:
        raise ValueError("Evaluation rhythm measurement source hash does not match the labelled audio")
    duration = _number(diagnosis.get("durationSeconds"), "report durationSeconds")
    if abs(duration - case["durationSeconds"]) > 0.00011:
        raise ValueError("Evaluation report duration does not match the labelled audio")
    events = diagnosis.get("events")
    if not isinstance(events, list) or len(events) > MAX_PREDICTIONS:
        raise ValueError("Evaluation prediction events must be a bounded list")
    parsed = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError("Evaluation prediction event must be an object")
        category, severity = event.get("category"), event.get("severity")
        if category not in (*CATEGORIES, "arrangement_break", "percussion_gap") or severity not in ("warning", "error", "info"):
            raise ValueError("Evaluation event category or severity is unsupported")
        start, end = _interval(event, case["durationSeconds"])
        if "confidence" in event and not 0 <= _number(event["confidence"], "event confidence") <= 1:
            raise ValueError("Event observation confidence must be within [0, 1]")
        parsed.append({"predictionIndex": index, "category": "backing_dropout" if category == "arrangement_break" else category,
                       "severity": severity, "startSeconds": start, "endSeconds": end})
    completeness = {"totalEventCount", "omittedEventCount", "omittedWarningCount", "eventsTruncated", "maximumEvents"}
    if not completeness <= set(diagnosis):
        return diagnosis, parsed, "event_completeness_unavailable"
    total = _integer(diagnosis["totalEventCount"], "totalEventCount")
    omitted = _integer(diagnosis["omittedEventCount"], "omittedEventCount")
    warnings = _integer(diagnosis["omittedWarningCount"], "omittedWarningCount")
    maximum = _integer(diagnosis["maximumEvents"], "maximumEvents")
    if not isinstance(diagnosis["eventsTruncated"], bool) or total != len(events) + omitted or warnings > omitted or diagnosis["eventsTruncated"] != (omitted > 0) or len(events) > maximum:
        raise ValueError("Evaluation event completeness metadata is inconsistent")
    return diagnosis, parsed, "predictions_truncated" if omitted else None


def _overlap_quality(annotation: dict[str, Any], prediction: dict[str, Any]) -> float | None:
    overlap = _intersection(annotation, prediction)
    annotation_duration = annotation["endSeconds"] - annotation["startSeconds"]
    prediction_duration = prediction["endSeconds"] - prediction["startSeconds"]
    union = annotation_duration + prediction_duration - overlap
    iou = overlap / union
    if iou + 1e-9 < 0.2 or overlap / annotation_duration + 1e-9 < 0.5 or overlap / prediction_duration + 1e-9 < 0.5:
        return None
    return iou


def _match(annotations: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, int]:
    """Deterministic augmenting paths maximize cardinality without double claims."""
    choices: dict[str, list[int]] = {}
    for annotation in annotations:
        edges = [(quality, prediction["predictionIndex"]) for prediction in predictions
                 if (quality := _overlap_quality(annotation, prediction)) is not None]
        choices[annotation["annotationId"]] = [index for _, index in sorted(edges, key=lambda edge: (-edge[0], edge[1]))]
    owners: dict[int, str] = {}

    # When one alert can match adjacent labels, ID spelling must not decide
    # whether a defect was found. Existing defect matches stay matched while
    # later augmenting paths attempt to explain additional intentional alerts.
    for annotation in sorted(annotations, key=lambda item: (item["label"] != "defect", item["annotationId"])):
        initial = annotation["annotationId"]
        queue = deque([initial])
        parents: dict[str, tuple[str, int]] = {}
        seen_annotations = {initial}
        seen_predictions: set[int] = set()
        found = None
        while queue and found is None:
            identifier = queue.popleft()
            for index in choices[identifier]:
                if index in seen_predictions:
                    continue
                seen_predictions.add(index)
                if index not in owners:
                    found = (identifier, index)
                    break
                owner = owners[index]
                if owner not in seen_annotations:
                    seen_annotations.add(owner)
                    parents[owner] = (identifier, index)
                    queue.append(owner)
        if found is not None:
            identifier, index = found
            while True:
                owners[index] = identifier
                if identifier not in parents:
                    break
                identifier, index = parents[identifier]
    return {identifier: index for index, identifier in owners.items()}


def _empty_counts() -> dict[str, int]:
    return {name: 0 for name in _COUNTS}


def _rates(counts: dict[str, int]) -> dict[str, float | None]:
    divide = lambda numerator, denominator: numerator / denominator if denominator else None
    return {"precision": divide(counts["truePositives"], counts["truePositives"] + counts["falsePositives"]),
            "recall": divide(counts["truePositives"], counts["defectAnnotations"]),
            "intentionalFalseAlertRate": divide(counts["intentionalFalseAlerts"], counts["intentionalAnnotations"]),
            "intentionalCompatibleObservationRate": divide(counts["intentionalCompatibleObservations"], counts["intentionalAnnotations"])}


def score_case(case: Any, report: Any) -> dict[str, Any]:
    """Score only source-bound, fully covered, determinate temporal truth."""
    case = validate_case(case)
    diagnosis, predictions, rejection = _prediction_data(case, report)
    determinate = [item for item in case["annotations"] if item["label"] != "uncertain"]
    status = "unscorable" if rejection else "scored" if determinate else "unlabelled"
    reason = rejection or ("no_determinate_labels" if not determinate else None)
    result = {"version": VERSION, "caseId": case["caseId"], "split": case["split"], "labelOrigin": case["labelOrigin"],
              "sourceArtifactSha256": case["sourceArtifactSha256"], "status": status, "reason": reason,
              "matchPolicy": dict(_MATCH_POLICY), "byCategory": {}, "matches": []}
    for key in ("parentArtifactSha256", "sourceFamily"):
        if key in case:
            result[key] = case[key]
    result["labelCounts"] = {label: sum(item["label"] == label for item in case["annotations"])
                             for label in ("defect", "intentional", "uncertain")}
    total_counts = _empty_counts()
    for category in CATEGORIES:
        counts = _empty_counts()
        annotations = [item for item in case["annotations"] if item["category"] == category]
        defects = [item for item in annotations if item["label"] == "defect"]
        intentions = [item for item in annotations if item["label"] == "intentional"]
        uncertain = [item for item in annotations if item["label"] == "uncertain"]
        category_predictions = [item for item in predictions if item["category"] == category]
        category_scored = status == "scored" and bool(defects or intentions)
        counts["uncertainAnnotations"] = len(uncertain)
        eligible = []
        for prediction in category_predictions:
            if not category_scored or not _reviewed(prediction, case["reviewedRanges"], category) or any(_intersection(prediction, item) > 0 for item in uncertain):
                counts["unscoredPredictions"] += 1
                counts["unscoredWarningPredictions"] += prediction["severity"] in ("warning", "error")
            else:
                eligible.append(prediction)
        if category_scored:
            warnings = [item for item in eligible if item["severity"] in ("warning", "error")]
            information = [item for item in eligible if item["severity"] == "info"]
            matches = _match(defects + intentions, warnings)
            info_matches = _match(defects + intentions, information)
            counts["defectAnnotations"], counts["intentionalAnnotations"] = len(defects), len(intentions)
            counts["reviewedWarningPredictions"] = len(warnings)
            counts["truePositives"] = sum(item["annotationId"] in matches for item in defects)
            counts["falsePositives"] = len(warnings) - counts["truePositives"]
            counts["falseNegatives"] = len(defects) - counts["truePositives"]
            # Large alerts cannot gain TPs, but still count when they cover an
            # intentional expression. Otherwise broad alerts would look safer.
            counts["intentionalFalseAlerts"] = sum(any(_intersection(item, prediction) / (item["endSeconds"] - item["startSeconds"]) >= 0.5 for prediction in warnings) for item in intentions)
            counts["intentionalCompatibleObservations"] = sum(item["annotationId"] in info_matches and not any(_intersection(item, prediction) / (item["endSeconds"] - item["startSeconds"]) >= 0.5 for prediction in warnings) for item in intentions)
            check_name = {"beat_timing": "beatTiming", "backing_dropout": "backingContinuity",
                          "mix_dropout": "mixContinuity"}[category]
            checks = diagnosis.get("checks") if isinstance(diagnosis.get("checks"), dict) else {}
            # Timing is abstained only when every timing check lacked evidence.
            names = [check_name, *(["hitTiming"] if category == "beat_timing" and "hitTiming" in checks else [])]
            unknown = all(isinstance(checks.get(name, {}), dict) and checks.get(name, {}).get("status") == "unknown"
                          for name in names)
            counts["unknownOrAbstainedDefects"] = sum(item["annotationId"] not in matches and (unknown or item["annotationId"] in info_matches) for item in defects)
            counts["undetectedDefects"] = counts["falseNegatives"] - counts["unknownOrAbstainedDefects"]
            result["matches"].extend({"annotationId": item["annotationId"], "label": item["label"], "category": category,
                                      "predictionIndex": matches[item["annotationId"]]} for item in defects + intentions if item["annotationId"] in matches)
        result["byCategory"][category] = {"counts": counts, "rates": _rates(counts)}
        for name in _COUNTS:
            total_counts[name] += counts[name]
    non_target = [item for item in predictions if item["category"] not in CATEGORIES]
    total_counts["unscoredPredictions"] += len(non_target)
    total_counts["unscoredWarningPredictions"] += sum(item["severity"] in ("warning", "error") for item in non_target)
    result["counts"] = total_counts
    result["rates"] = _rates(total_counts)
    return result


def aggregate_scores(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep human/injected populations and calibration/holdout results separate."""
    if not isinstance(results, list) or len(results) > MAX_CASES:
        raise ValueError("Evaluation results must be a bounded list")
    identifiers = set()
    source_splits: dict[tuple[str, str], str] = {}
    groups: dict[str, Any] = {}
    for result in results:
        if not isinstance(result, dict) or result.get("version") != VERSION or result.get("status") not in ("scored", "unscorable", "unlabelled"):
            raise ValueError("Aggregate requires validated rhythm evaluation results")
        identifier = _text(result.get("caseId"), "caseId")
        if identifier in identifiers:
            raise ValueError("Aggregate case IDs must be unique")
        identifiers.add(identifier)
        split, origin = result.get("split"), result.get("labelOrigin")
        if split not in ("calibration", "holdout") or origin not in ("human", "controlled_injection"):
            raise ValueError("Aggregate result split or label origin is invalid")
        for field in ("sourceArtifactSha256", "parentArtifactSha256", "sourceFamily"):
            if field == "sourceArtifactSha256" or field in result:
                source = _text(result.get(field), field, 4096)
                if field.endswith("Sha256") and not _SHA256.fullmatch(source):
                    raise ValueError("Aggregate source hash is invalid")
                identity = (field, source)
                if identity in source_splits and source_splits[identity] != split:
                    raise ValueError("Aggregate source families must not cross calibration and holdout splits")
                source_splits[identity] = split
        key = f"{origin}:{split}"
        group = groups.setdefault(key, {"labelOrigin": origin, "split": split, "caseCount": 0, "scoredCaseCount": 0,
                                       "unscorableCaseCount": 0, "unlabelledCaseCount": 0,
                                       "counts": _empty_counts(), "byCategory": {category: {"counts": _empty_counts()} for category in CATEGORIES}})
        group["caseCount"] += 1
        group[{"scored": "scoredCaseCount", "unscorable": "unscorableCaseCount", "unlabelled": "unlabelledCaseCount"}[result["status"]]] += 1
        for target, supplied in [(group["counts"], result.get("counts")), *[(group["byCategory"][category]["counts"], result.get("byCategory", {}).get(category, {}).get("counts")) for category in CATEGORIES]]:
            if not isinstance(supplied, dict) or set(supplied) != set(_COUNTS):
                raise ValueError("Aggregate result counts are incomplete")
            for name in _COUNTS:
                _integer(supplied[name], name)
            if (supplied["truePositives"] + supplied["falseNegatives"] != supplied["defectAnnotations"]
                    or supplied["truePositives"] + supplied["falsePositives"] != supplied["reviewedWarningPredictions"]
                    or supplied["unknownOrAbstainedDefects"] + supplied["undetectedDefects"] != supplied["falseNegatives"]
                    or supplied["intentionalFalseAlerts"] + supplied["intentionalCompatibleObservations"] > supplied["intentionalAnnotations"]):
                raise ValueError("Aggregate result counts are inconsistent")
            for name in _COUNTS:
                target[name] += _integer(supplied[name], name)
    for group in groups.values():
        group["rates"] = _rates(group["counts"])
        for category in CATEGORIES:
            group["byCategory"][category]["rates"] = _rates(group["byCategory"][category]["counts"])
    return {"version": VERSION, "caseCount": len(results), "groups": {key: groups[key] for key in sorted(groups)},
            "matchPolicy": dict(_MATCH_POLICY),
            "interpretation": "Human labels and controlled interventions have separate metrics; absence of labels establishes no measured accuracy. Observation confidence is not a defect probability."}
