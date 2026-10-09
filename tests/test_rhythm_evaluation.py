"""Empirical metrics neither invent labels nor reward duplicated/broad alerts."""

from copy import deepcopy

import pytest

from local_music_engine.rhythm_evaluation import VERSION, aggregate_scores, score_case, validate_manifest


HASH = "a" * 64


def annotation(identifier="d1", *, start=2, end=4, category="beat_timing", label="defect", origin="human"):
    return {"annotationId": identifier, "category": category, "label": label, "startSeconds": start, "endSeconds": end,
            "evidence": {"kind": "human_listening" if origin == "human" else "controlled_injection",
                         "description": "Explicit listening judgement" if origin == "human" else "Known injected perturbation",
                         "sourceRef": "fixture:review-1"}}


def case(annotations=None, *, origin="human", split="holdout", identifier="case-1", regions=None):
    return {"caseId": identifier, "split": split, "labelOrigin": origin, "sourceArtifactSha256": HASH,
            "durationSeconds": 10, "reviewedRanges": regions if regions is not None else [
                {"startSeconds": 0, "endSeconds": 10, "categories": ["beat_timing", "backing_dropout"]}],
            "annotations": annotations if annotations is not None else [annotation(origin=origin)]}


def event(start=2, end=4, *, severity="warning", category="beat_timing"):
    return {"startSeconds": start, "endSeconds": end, "severity": severity, "category": category,
            "confidence": 0.9, "retryEligible": False}


def report(events=None, *, status="observed"):
    events = [] if events is None else events
    return {"sourceArtifactSha256": HASH, "diagnostics": {"version": "rhythm-diagnostics-v2", "durationSeconds": 10,
            "events": events, "totalEventCount": len(events), "omittedEventCount": 0, "omittedWarningCount": 0,
            "eventsTruncated": False, "maximumEvents": 64,
            "checks": {"beatTiming": {"status": status}, "backingContinuity": {"status": status}}}}


@pytest.mark.parametrize("version", [None, "future", True])
def test_unsupported_diagnostic_version_cannot_establish_measured_accuracy(version):
    supplied = report([event()])
    supplied["diagnostics"]["version"] = version
    with pytest.raises(ValueError, match="version"):
        score_case(case(), supplied)


def test_duplicate_warnings_cannot_both_claim_the_same_defect():
    result = score_case(case(), report([event(), event()]))
    assert result["counts"]["truePositives"] == 1
    assert result["counts"]["falsePositives"] == 1
    assert result["rates"]["precision"] == 0.5
    assert result["rates"]["recall"] == 1
    assert len(result["matches"]) == 1


def test_one_prediction_cannot_claim_two_adjacent_defects():
    labels = [annotation("a", start=1, end=2), annotation("b", start=2, end=3)]
    result = score_case(case(labels), report([event(1, 3)]))
    assert result["counts"]["truePositives"] == 1
    assert result["counts"]["falseNegatives"] == 1
    assert result["rates"]["recall"] == 0.5


def test_label_id_spelling_cannot_make_a_defect_lose_a_shared_warning():
    labels = [annotation("z-defect", start=1, end=2), annotation("a-intent", start=2, end=3, label="intentional")]
    result = score_case(case(labels), report([event(1, 3)]))
    assert result["counts"]["truePositives"] == 1
    assert result["counts"]["intentionalFalseAlerts"] == 1


def test_augmenting_paths_find_two_matches_without_greedy_under_counting():
    labels = [annotation("a", start=1, end=2), annotation("b", start=2, end=3)]
    result = score_case(case(labels), report([event(1, 3), event(0.5, 1.5)]))
    assert result["counts"]["truePositives"] == 2
    assert result["counts"]["falsePositives"] == 0
    assert score_case(case(list(reversed(labels))), report([event(1, 3), event(0.5, 1.5)]))["matches"] == result["matches"]


@pytest.mark.parametrize("prediction", [event(0, 10), event(2, 2.1), event(3.99, 5)])
def test_oversized_tiny_or_edge_only_overlap_does_not_earn_detection(prediction):
    result = score_case(case(), report([prediction]))
    assert result["counts"]["truePositives"] == 0
    assert result["counts"]["falsePositives"] == 1
    assert result["counts"]["falseNegatives"] == 1


def test_uncertain_and_unreviewed_predictions_remain_unscored():
    labels = [annotation(start=1, end=2), annotation("u", start=3, end=4, label="uncertain")]
    regions = [{"startSeconds": 0, "endSeconds": 5, "categories": ["beat_timing"]}]
    result = score_case(case(labels, regions=regions), report([event(1, 2), event(3, 4), event(7, 8), event(4, 6)]))
    assert result["counts"]["truePositives"] == 1
    assert result["counts"]["falsePositives"] == 0
    assert result["counts"]["unscoredWarningPredictions"] == 3
    assert result["counts"]["uncertainAnnotations"] == 1


def test_unlabelled_category_does_not_gain_measured_accuracy_from_another_category():
    result = score_case(case(), report([event(), event(category="backing_dropout")]))
    assert result["counts"]["falsePositives"] == 0
    assert result["counts"]["unscoredWarningPredictions"] == 1
    assert result["byCategory"]["backing_dropout"]["rates"]["precision"] is None


@pytest.mark.parametrize("status, predictions, abstained", [
    ("observed", [], 0), ("unknown", [], 1), ("observed", [event(severity="info")], 1),
])
def test_absence_unknown_and_downgraded_defects_all_remain_misses(status, predictions, abstained):
    result = score_case(case(), report(predictions, status=status))
    assert result["counts"]["falseNegatives"] == 1
    assert result["counts"]["unknownOrAbstainedDefects"] == abstained
    assert result["counts"]["undetectedDefects"] == 1 - abstained
    assert result["rates"]["recall"] == 0
    assert result["rates"]["precision"] is None


def test_intentional_false_alerts_are_measured_separately_from_defect_recall():
    labels = [annotation(), annotation("i", start=6, end=8, label="intentional")]
    warning = score_case(case(labels), report([event(), event(6, 8)]))
    information = score_case(case(labels), report([event(), event(6, 8, severity="info")]))
    assert warning["rates"]["precision"] == 0.5
    assert warning["rates"]["recall"] == 1
    assert warning["rates"]["intentionalFalseAlertRate"] == 1
    assert information["rates"]["precision"] == 1
    assert information["rates"]["intentionalFalseAlertRate"] == 0
    assert information["rates"]["intentionalCompatibleObservationRate"] == 1


def test_broad_warning_cannot_hide_its_alert_over_intended_expression():
    labels = [annotation(), annotation("i", start=6, end=8, label="intentional")]
    result = score_case(case(labels), report([event(0, 10)]))
    assert result["counts"]["truePositives"] == 0
    assert result["rates"]["intentionalFalseAlertRate"] == 1


def test_arrangement_info_is_compared_with_backing_intent():
    label = annotation(category="backing_dropout", label="intentional")
    result = score_case(case([label]), report([event(category="arrangement_break", severity="info")]))
    assert result["counts"]["intentionalCompatibleObservations"] == 1
    assert result["rates"]["intentionalFalseAlertRate"] == 0


@pytest.mark.parametrize("status, prediction, expected_abstained", [
    ("unknown", None, 1), ("observed", None, 0), ("observed", "info", 1),
])
def test_mix_dropouts_missing_unknown_or_downgraded_still_count_as_misses(status, prediction, expected_abstained):
    labels = [annotation(category="mix_dropout")]
    regions = [{"startSeconds": 0, "endSeconds": 10, "categories": ["mix_dropout"]}]
    measured = report([] if prediction is None else [event(category="mix_dropout", severity=prediction)])
    measured["diagnostics"]["checks"]["mixContinuity"] = {"status": status}
    result = score_case(case(labels, regions=regions), measured)
    assert result["counts"]["falseNegatives"] == 1
    assert result["counts"]["unknownOrAbstainedDefects"] == expected_abstained
    assert result["rates"]["recall"] == 0


def test_mix_dropout_warning_can_match_only_the_full_mix_label_category():
    labels = [annotation(category="mix_dropout")]
    regions = [{"startSeconds": 0, "endSeconds": 10, "categories": ["mix_dropout"]}]
    result = score_case(case(labels, regions=regions), report([event(category="mix_dropout")]))
    assert result["byCategory"]["mix_dropout"]["counts"]["truePositives"] == 1
    assert result["rates"]["recall"] == 1


@pytest.mark.parametrize("labels", [[], [annotation(label="uncertain")]])
def test_no_determinate_labels_means_no_measured_accuracy(labels):
    result = score_case(case(labels), report([event()]))
    assert result["status"] == "unlabelled"
    assert all(value is None for value in result["rates"].values())
    assert result["counts"]["unscoredWarningPredictions"] == 1
    assert result["counts"]["falsePositives"] == 0


@pytest.mark.parametrize("warning_count", [0, 1])
def test_any_event_cap_omission_makes_the_case_unscorable(warning_count):
    measured = report([event()])
    measured["diagnostics"].update(totalEventCount=2, omittedEventCount=1,
                                   omittedWarningCount=warning_count, eventsTruncated=True)
    result = score_case(case(), measured)
    assert result["status"] == "unscorable"
    assert result["reason"] == "predictions_truncated"
    assert all(value is None for value in result["rates"].values())


def test_missing_cap_provenance_is_unscorable_and_inconsistent_counts_are_rejected():
    measured = report([event()])
    del measured["diagnostics"]["totalEventCount"]
    assert score_case(case(), measured)["reason"] == "event_completeness_unavailable"
    measured = report([event()])
    measured["diagnostics"]["totalEventCount"] = 2
    with pytest.raises(ValueError, match="completeness"):
        score_case(case(), measured)


def test_source_and_measurement_hashes_and_duration_must_match_labels():
    for change in ({"sourceArtifactSha256": "b" * 64},):
        measured = report([event()])
        measured.update(change)
        with pytest.raises(ValueError, match="hash"):
            score_case(case(), measured)
    measured = report([event()])
    measured["diagnostics"]["durationSeconds"] = 11
    with pytest.raises(ValueError, match="duration"):
        score_case(case(), measured)
    measured = {"sourceArtifactSha256": HASH, "rhythm": {
        "measuredArtifactSha256": "b" * 64, "diagnostics": report([event()])["diagnostics"]}}
    with pytest.raises(ValueError, match="hash"):
        score_case(case(), measured)


def test_aggregate_keeps_human_and_injection_and_splits_separate():
    human = score_case(case(identifier="human"), report([]))
    injected_case = case(origin="controlled_injection", split="calibration", identifier="injection")
    injected_case["sourceArtifactSha256"] = "b" * 64
    injected_report = report([event()])
    injected_report["sourceArtifactSha256"] = "b" * 64
    injection = score_case(injected_case, injected_report)
    summary = aggregate_scores([human, injection])
    assert set(summary["groups"]) == {"human:holdout", "controlled_injection:calibration"}
    assert summary["groups"]["human:holdout"]["rates"]["recall"] == 0
    assert summary["groups"]["controlled_injection:calibration"]["rates"]["recall"] == 1
    assert "rates" not in summary


@pytest.mark.parametrize("change", [
    {"durationSeconds": True}, {"durationSeconds": float("inf")}, {"durationSeconds": 10**500},
    {"sourceArtifactSha256": "bad"}, {"labelOrigin": "model_guess"}, {"split": "training"},
    {"annotations": [annotation(), annotation()]},
    {"annotations": [annotation(), annotation("overlap", start=3, end=5)]},
    {"reviewedRanges": [{"startSeconds": 0, "endSeconds": 1, "categories": ["beat_timing"]}]},
    {"reviewedRanges": [{"startSeconds": 0, "endSeconds": 10, "categories": ["beat_timing", "beat_timing"]}]},
    {"modelCertainty": True},
])
def test_malformed_truth_manifest_is_rejected(change):
    supplied = case()
    supplied.update(change)
    with pytest.raises(ValueError):
        validate_manifest({"version": VERSION, "cases": [supplied]})


def test_provenance_cannot_mix_human_truth_with_injected_labels():
    supplied = case()
    supplied["annotations"][0]["evidence"]["kind"] = "controlled_injection"
    with pytest.raises(ValueError, match="labelOrigin"):
        score_case(supplied, report())


def test_duplicate_case_ids_and_cross_split_source_leakage_are_rejected():
    with pytest.raises(ValueError, match="IDs"):
        validate_manifest({"version": VERSION, "cases": [case(), case()]})
    with pytest.raises(ValueError, match="splits"):
        validate_manifest({"version": VERSION, "cases": [case(), case(identifier="other", split="calibration")]})
    result = score_case(case(), report())
    with pytest.raises(ValueError, match="IDs"):
        aggregate_scores([result, deepcopy(result)])


def test_optional_lineage_and_io_metadata_are_preserved_and_validated():
    labelled = case()
    labelled.update(audioPath="audio/holdout.wav", audioBytes=32044, intentPath="intent.json", stemLabel="mix",
                    stemCachePath="cache.json", evidenceAudioPath="source.wav", reportPath="report.json",
                    parentArtifactSha256="b" * 64, sourceFamily="source-family-1")
    validated = validate_manifest({"version": VERSION, "cases": [labelled]})["cases"][0]
    assert validated["audioBytes"] == 32044
    assert validated["stemLabel"] == "mix"
    result = score_case(labelled, report())
    assert result["parentArtifactSha256"] == "b" * 64
    assert result["labelCounts"]["defect"] == 1
    other = deepcopy(labelled)
    other.update(caseId="related", sourceArtifactSha256="c" * 64, split="calibration")
    with pytest.raises(ValueError, match="families"):
        validate_manifest({"version": VERSION, "cases": [labelled, other]})


def test_aggregate_rejects_forged_counts_and_split_leakage():
    result = score_case(case(), report([event()]))
    forged = deepcopy(result)
    forged["counts"]["truePositives"] = 2
    with pytest.raises(ValueError, match="inconsistent"):
        aggregate_scores([forged])
    leaked = deepcopy(result)
    leaked.update(caseId="another", split="calibration")
    with pytest.raises(ValueError, match="splits"):
        aggregate_scores([result, leaked])


@pytest.mark.parametrize("prediction", [event(start=True), event(end=float("nan")), event(end=11),
                                        event(severity="approved"), event(category="model_error")])
def test_invalid_prediction_intervals_and_categories_are_rejected(prediction):
    with pytest.raises(ValueError):
        score_case(case(), report([prediction]))
