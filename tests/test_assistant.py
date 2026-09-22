import json

import pytest

from local_music_engine.assistant import (
    AssistantError,
    LlmConfig,
    PlanRequest,
    catalog,
    diff_tags,
    plan_from_llm_answer,
    plan_revision,
    rule_plan,
)

CAPTION = "Korean indie pop, warm female vocal, gentle electric piano, restrained drums"


def request(feedback: str, **overrides) -> PlanRequest:
    values = {
        "feedback": feedback,
        "caption": CAPTION,
        "lyrics": "[Verse]\n가사",
        "duration_seconds": 120.0,
    }
    values.update(overrides)
    return PlanRequest(**values)


def test_selected_range_becomes_repaint_with_caption_change() -> None:
    plan = rule_plan(
        request("후렴 발음이 뭉개져요", edit_range={"startSeconds": 30, "endSeconds": 42})
    )
    assert plan["action"] == "repaint"
    assert plan["range"] == {"startSeconds": 30.0, "endSeconds": 42.0}
    assert "clear Korean diction" in plan["stylePrompt"]
    assert plan["stylePrompt"].startswith(CAPTION)
    assert {"op": "add", "term": "clear Korean diction", "label": "발음을 또렷하게"} in plan["changes"]
    assert plan["understood"] is True


def test_song_wide_feedback_regenerates_and_replaces_conflicting_tags() -> None:
    plan = rule_plan(request("드럼이 너무 세요", versions=3))
    assert plan["action"] == "regenerate"
    assert plan["versions"] == 3
    assert "restrained drums" not in plan["stylePrompt"]
    assert "light drums" in plan["stylePrompt"]


def test_vocal_gender_swap_does_not_confuse_female_and_male() -> None:
    plan = rule_plan(request("남자 목소리로 바꿔 주세요"))
    assert "warm female vocal" not in plan["stylePrompt"]
    assert "male vocal" in plan["stylePrompt"]
    back = rule_plan(request("여자 보컬로", caption="city pop, male vocal"))
    assert back["stylePrompt"] == "city pop, female vocal"


def test_ending_feedback_picks_the_tail_when_no_range_is_selected() -> None:
    plan = rule_plan(request("끝이 갑자기 끊겨요"))
    assert plan["action"] == "repaint"
    assert plan["range"] == {"startSeconds": 110.0, "endSeconds": 120.0}


def test_unrecognized_feedback_is_reported_not_guessed() -> None:
    plan = rule_plan(request("음 뭔가 좀 그래요"))
    assert plan["understood"] is False
    assert plan["stylePrompt"] == CAPTION
    assert plan["changes"] == []
    assert plan["notes"]


def test_tempo_change_moves_known_bpm_only_for_new_versions() -> None:
    assert rule_plan(request("너무 느려요", bpm=92))["bpm"] == 107
    ranged = rule_plan(
        request("너무 느려요", bpm=92, edit_range={"startSeconds": 0, "endSeconds": 10})
    )
    assert ranged["bpm"] is None


def test_catalog_examples_are_understood_by_their_own_rule() -> None:
    for entry in catalog():
        plan = rule_plan(request(entry["example"]))
        labels = {change.get("label") for change in plan["changes"]}
        assert plan["understood"], entry
        assert entry["label"] in labels, entry


def test_llm_answer_is_validated_and_diffed() -> None:
    answer = {
        "action": "regenerate",
        "summary": "보컬을 앞으로",
        "caption": CAPTION + ", prominent lead vocal",
        "lyrics": None,
        "bpm": None,
        "strength": "medium",
        "labels": [{"en": "prominent lead vocal", "ko": "보컬 크게"}],
    }
    plan = plan_from_llm_answer(json.dumps(answer), request("보컬이 작아요"), model="qwen")
    assert plan["assistant"] == "llm"
    assert plan["changes"] == [{"op": "add", "term": "prominent lead vocal", "label": "보컬 크게"}]


def test_llm_repaint_without_any_range_falls_back_to_regenerate() -> None:
    answer = {"action": "repaint", "summary": "", "caption": CAPTION, "strength": "light"}
    plan = plan_from_llm_answer(json.dumps(answer), request("음"), model="qwen")
    assert plan["action"] == "regenerate"
    assert plan["range"] is None


def test_llm_answer_with_unknown_action_is_rejected() -> None:
    with pytest.raises(AssistantError):
        plan_from_llm_answer('{"action": "delete", "caption": "x"}', request("음"), model="m")


def test_unreachable_llm_falls_back_to_rules_with_a_note() -> None:
    config = LlmConfig(base_url="http://127.0.0.1:9", model="none", timeout_seconds=1)
    plan = plan_revision(request("드럼이 너무 세요"), config)
    assert plan["assistant"] == "rules"
    assert "LLM" in plan["notes"][0]


def test_llm_must_be_loopback() -> None:
    with pytest.raises(ValueError, match="loopback"):
        LlmConfig(base_url="http://example.com", model="m").validated()


def test_diff_tags_is_case_insensitive() -> None:
    assert diff_tags("Pop, Piano", "pop, piano, strings") == [{"op": "add", "term": "strings"}]
