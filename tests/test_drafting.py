import json
import math
from typing import Any

from local_music_engine.assistant import LlmConfig, rule_plan, PlanRequest
from local_music_engine.drafting import (
    draft_from_llm_answer,
    draft_song,
    engine_draft,
    keyword_tags,
    usable_korean_lyrics,
)


class FakeSampleClient:
    queries: list[str] = []
    lyrics = "[Verse]\n[ko] nega iss-eo oneuldo\n[ko] neo eobs-i"

    def __init__(self, base_url: str):
        self.base_url = base_url

    def create_sample(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(query)
        return {"caption": "A calm piano ballad with a soft female vocal.", "lyrics": self.lyrics, "duration": 150}


def test_korean_description_becomes_english_tags_for_the_engine() -> None:
    tags = keyword_tags("비 오는 밤 혼자 걷는 느낌의 잔잔한 발라드, 여자 목소리, 피아노 중심")
    assert tags[0] == "Korean ballad"
    assert {"calm", "rainy mood", "night", "lonely", "female vocal", "piano"} <= set(tags)
    assert "male vocal" not in tags


def test_romanized_engine_lyrics_are_dropped_and_reported() -> None:
    FakeSampleClient.queries = []
    result = engine_draft("잔잔한 발라드", instrumental=False, base_url="http://127.0.0.1:1", client_factory=FakeSampleClient)
    assert FakeSampleClient.queries == ["Korean ballad, calm"]
    assert result["lyrics"] == ""
    assert any("한글" in note for note in result["notes"])
    assert result["bpm"] is None and result["keyScale"] is None


def test_hangul_lyrics_are_kept_without_language_markers() -> None:
    assert usable_korean_lyrics("[Verse]\n[ko] 새벽빛이 내려와\n[ko] 마음을 깨우네") == "[Verse]\n새벽빛이 내려와\n마음을 깨우네"


def test_llm_draft_must_be_hangul() -> None:
    good = {"title": "비 오는 밤", "caption": "Korean ballad, calm, female vocal, piano", "lyrics": "[Verse]\n빗소리에 걸어요\n[Chorus]\n혼자라도 괜찮아", "durationSeconds": 120}
    result = draft_from_llm_answer(json.dumps(good, ensure_ascii=False), instrumental=False, model="qwen")
    assert result["source"] == "llm" and result["title"] == "비 오는 밤"
    bad = dict(good, lyrics="[Verse]\nbitsoli-e geol-eoyo")
    try:
        draft_from_llm_answer(json.dumps(bad), instrumental=False, model="qwen")
    except Exception as error:
        assert "Hangul" in str(error)
    else:
        raise AssertionError("romanized lyrics must be rejected")


def test_unreachable_llm_falls_back_to_engine_draft() -> None:
    config = LlmConfig(base_url="http://127.0.0.1:9", model="none", timeout_seconds=1)
    result = draft_song("잔잔한 발라드", instrumental=False, duration_seconds=60, base_url="http://127.0.0.1:1", llm=config, client_factory=FakeSampleClient)
    assert result["source"] == "engine"
    assert "LLM" in result["notes"][0]


def test_rules_only_append_to_prose_captions() -> None:
    prose = "A calm piano ballad with restrained drums, a soft female vocal and a warm, intimate mix."
    plan = rule_plan(PlanRequest(feedback="드럼이 너무 세요", caption=prose, lyrics="가사", duration_seconds=60))
    assert plan["stylePrompt"].startswith("A calm piano ballad with restrained drums")
    assert plan["stylePrompt"].endswith("light drums")
    assert all(change["op"] == "add" for change in plan["changes"])
    assert not math.isnan(len(plan["notes"]))


def test_llm_draft_unescapes_literal_newlines() -> None:
    answer = {"title": "밤", "caption": "Korean ballad", "lyrics": "[Verse]\\n빗소리\\n[Chorus]\\n괜찮아"}
    result = draft_from_llm_answer(json.dumps(answer, ensure_ascii=False), instrumental=False, model="qwen")
    assert result["lyrics"] == "[Verse]\n빗소리\n[Chorus]\n괜찮아"
