import copy
import json
import math
import re

import pytest

from local_music_engine import drafting, production_rules
from local_music_engine.assistant import LlmConfig
from local_music_engine.song_planning import count_syllables, prepare_song_plan, validate_song_plan_input


def plan(lyrics: str, **changes):
    values = dict(duration_seconds=30, bpm=None, time_signature=None, preset_id="emotional-hiphop",
                  instrumental=False, development=True, breathing=True)
    values.update(changes)
    return prepare_song_plan(lyrics, **values)


def sung_characters(lyrics: str) -> str:
    without_tags = re.sub(r"^\s*\[[^\]\r\n]+\]\s*$", "", lyrics, flags=re.MULTILINE)
    return re.sub(r"\s+", "", without_tags)


@pytest.mark.parametrize("lyrics", [
    "[Verse]\n오늘 밤 혼자 걷는 길에 네가 남긴 말이 생각나서 다시 하늘을 바라봐\n[Chorus]\n조금 천천히 내게 돌아와",
    "[Verse]\nI keep your voice beside me through the quiet city streets, and every little memory brings me home.\n[Chorus]\nStay here, don't let me go",
    "[Verse]\n오늘도 I remember your smile 내 마음에 남아 있는 작은 빛을 따라 걸어\n[Chorus]\nShine on me, 내 곁에",
])
def test_phrase_preparation_preserves_every_lyric_character_and_word_order(lyrics: str) -> None:
    result = plan(lyrics)
    assert result["lyricsOriginal"] == lyrics
    assert sung_characters(result["lyricsPrepared"]) == sung_characters(lyrics)
    assert any(change["kind"] == "line-break" for change in result["changes"])
    assert len(result["phrases"]) > 2
    assert all(phrase["estimatedBars"] >= 0.5 and phrase["breathAfterBeats"] > 0 for phrase in result["phrases"])
    assert all(phrase["syllableMethod"] == "hangul-exact+english-heuristic" for phrase in result["phrases"])
    assert result == plan(lyrics)


def test_maximum_length_lyrics_preserve_words_across_many_safe_breaks() -> None:
    lyrics = ("네 곁에 stay with me, " * 300)[:4096]
    result = plan(lyrics, duration_seconds=10, bpm=30, time_signature="2")
    assert result["lyricsOriginal"] == lyrics
    assert sung_characters(result["lyricsPrepared"]) == sung_characters(lyrics)
    assert len(result["phrases"]) > 100


def test_disabled_rules_keep_raw_line_endings_and_manual_tags_exactly() -> None:
    lyrics = " [Verse - whispered] \r\n  오늘 밤 곁에 있어 줘  \r\n\r\n[Chorus]\r\nStay with me\r\n"
    result = plan(lyrics, development=False, breathing=False)
    assert result["lyricsOriginal"] == result["lyricsPrepared"] == lyrics
    assert result["arrangement"] == [] and result["changes"] == []


def test_development_preserves_manual_tag_order_and_never_fabricates_sections() -> None:
    lyrics = "[Chorus: whispered]\nKeep me close\n[My own section]\n바람 소리\n[Verse 2]\nStay with me"
    result = plan(lyrics, breathing=False)
    prepared = result["lyricsPrepared"]
    assert "[Chorus: whispered]" in prepared and "[My own section]" in prepared
    assert "[Verse 2 - restrained]" in prepared
    assert prepared.index("Keep me close") < prepared.index("바람 소리") < prepared.index("Stay with me")
    assert "[Outro" not in prepared and "[Intro" not in prepared
    assert len(result["changes"]) == 1 and result["changes"][0]["kind"] == "section-guidance"
    # The layout is a suggestion, rather than a reassignment of existing chorus text.
    assert result["arrangement"][0]["label"] == "Verse"
    repeated = plan(prepared, breathing=False)
    assert repeated["lyricsPrepared"] == prepared
    assert repeated["changes"] == []


def test_unsplittable_hangul_and_english_words_remain_whole_and_are_reported() -> None:
    korean = "기다리는마음으로너의이름을부르며밤하늘을바라본다"
    english = "supercalifragilisticexpialidocious"
    result = plan(f"[Verse]\n{korean}\n{english}")
    assert korean in result["lyricsPrepared"] and english in result["lyricsPrepared"]
    assert result["phrases"][0]["syllables"] == len(korean)
    assert any("안전하게 더 나눌 수 없는" in warning for warning in result["warnings"])
    assert count_syllables("네가 그리워") == 5
    assert count_syllables("네가 stay with me") == 5
    assert any("영어 음절 수" in warning for warning in result["warnings"])


def test_punctuation_is_a_safe_boundary_but_apostrophes_and_decimals_are_retained() -> None:
    lyrics = "[Verse]\nDon't leave me now,keep the tiny 3.14 light beside you;hold me through another quiet night."
    result = plan(lyrics, development=False)
    assert "Don't" in result["lyricsPrepared"] and "3.14" in result["lyricsPrepared"]
    assert sung_characters(lyrics) == sung_characters(result["lyricsPrepared"])
    assert all("\n" not in phrase["text"] for phrase in result["phrases"])


@pytest.mark.parametrize("duration,bpm,meter", [(10, 30, "2"), (30, 84, "4/4"), (46, 30, "4"), (600, 300, "6/8")])
def test_arrangement_has_finite_contiguous_ranges_within_requested_length(duration, bpm, meter) -> None:
    result = plan("[Verse]\n내 곁에\n[Chorus]\n돌아와", duration_seconds=duration, bpm=bpm, time_signature=meter)
    sections = result["arrangement"]
    assert sections[0]["startSeconds"] == 0 and sections[-1]["endSeconds"] == duration
    for index, section in enumerate(sections):
        assert 0 <= section["startSeconds"] < section["endSeconds"] <= duration
        assert math.isfinite(section["bars"]) and section["bars"] > 0
        assert 0 <= section["energy"] <= 1
        if index:
            assert sections[index - 1]["endSeconds"] == section["startSeconds"]
    json.dumps(result, allow_nan=False)
    if meter == "6/8":
        assert result["timing"]["timeSignature"] == "6"
        assert result["timing"]["quarterBeatsPerBar"] == 3
        assert result["timing"]["bpmBeatUnit"] == "quarter-note"
    if duration == 600:
        assert [section["label"] for section in sections] == ["Intro", "Verse", "Pre-Chorus", "Chorus", "Verse", "Chorus", "Outro"]


def test_preset_tempo_and_phrase_targets_resolve_consistently_and_density_is_advisory() -> None:
    lyrics = "[Verse]\n" + "우리 함께 " * 100
    hiphop = plan(lyrics)
    pop = plan(lyrics, preset_id="upbeat-pop")
    slow = plan(lyrics, bpm=30)
    fast = plan(lyrics, bpm=300)
    assert hiphop["timing"]["bpm"] == 84 and pop["timing"]["bpm"] == 120
    assert slow["phrases"][0]["targetSyllables"] < fast["phrases"][0]["targetSyllables"]
    assert hiphop["phrases"][0]["breathAfterBeats"] != pop["phrases"][0]["breathAfterBeats"]
    assert any("촘촘" in warning for warning in hiphop["warnings"])
    assert sung_characters(lyrics) == sung_characters(hiphop["lyricsPrepared"])


def test_instrumental_has_no_lyric_phrase_rewrite_or_breathing_directive() -> None:
    lyrics = "[Instrumental]"
    result = plan(lyrics, instrumental=True)
    assert result["lyricsPrepared"] == lyrics and result["phrases"] == []
    assert [section["label"] for section in result["arrangement"]] == ["Theme", "Theme reprise", "Outro"]
    assert not any("syllables" in item for item in result["guidance"])
    frozen = production_rules.freeze_guidance({"version": 1, "presetId": None, "ruleIds": ["phrase-breathing"]},
                                               base_style="instrumental", duration_seconds=30, instrumental=True)
    assert frozen["appliedRuleIds"] == [] and frozen["skippedRuleIds"] == ["phrase-breathing"]


def valid_input():
    return {"lyrics": "[Verse]\n노래", "durationSeconds": 30, "bpm": None, "timeSignature": None,
            "presetId": "emotional-hiphop", "instrumental": False, "development": True, "breathing": True}


@pytest.mark.parametrize("field,value", [("lyrics", 4), ("lyrics", "x" * 4097), ("durationSeconds", True),
    ("durationSeconds", 9), ("durationSeconds", 601), ("durationSeconds", float("nan")),
    ("bpm", True), ("bpm", "84"), ("bpm", 0), ("bpm", float("inf")),
    ("timeSignature", False), ("timeSignature", "5/4"), ("timeSignature", 4.0),
    ("presetId", "unknown"), ("development", 1), ("breathing", None), ("instrumental", "false")])
def test_untrusted_plan_inputs_are_rejected_before_preparation(field, value) -> None:
    request = valid_input()
    request[field] = value
    with pytest.raises(ValueError):
        validate_song_plan_input(request)


def test_plan_input_validation_is_strict_and_never_mutates_input() -> None:
    request = valid_input()
    original = copy.deepcopy(request)
    validated = validate_song_plan_input(request)
    assert request == original and validated == request and validated is not request
    with pytest.raises(ValueError):
        validate_song_plan_input(dict(request, unknown=True))
    request.pop("bpm")
    with pytest.raises(ValueError):
        validate_song_plan_input(request)


def test_new_draft_rules_send_actual_section_and_breathing_advice_without_overwriting_raw_lyrics(monkeypatch) -> None:
    captured = []
    raw_lyrics = "[Verse]\n오늘 밤 너와 함께 걷는 길에 작은 별이 우리 곁에 내려와\n[Chorus]\n내 곁에 머물러"
    class SampleClient:
        def __init__(self, base_url):
            pass
        def create_sample(self, query, **kwargs):
            captured.append(query)
            return {"caption": "emotional hip hop", "lyrics": raw_lyrics, "duration": 30}
    controls = {"version": 1, "presetId": "emotional-hiphop", "ruleIds": ["section-development", "phrase-breathing"]}
    common = dict(duration_seconds=30, instrumental=False, production_rules=controls,
                  base_url="http://127.0.0.1:1", client_factory=SampleClient)
    engine = drafting.draft_song("감성 힙합", **common, engine="ace-step")
    assert "breathing space" in captured[0] and "Short verse to fuller hook" in captured[0]
    assert engine["lyrics"] == raw_lyrics
    assert engine["songPlan"]["lyricsOriginal"] == raw_lyrics
    assert engine["songPlan"]["changes"]
    def chat(config, messages, **kwargs):
        captured.append(json.loads(messages[1]["content"]))
        return json.dumps({"caption": "emotional hip hop", "lyrics": raw_lyrics}, ensure_ascii=False)
    monkeypatch.setattr(drafting, "_chat", chat)
    llm = drafting.draft_song("감성 힙합", llm=LlmConfig(base_url="http://127.0.0.1:11434", model="test"), **common, engine="ace-step")
    assert captured[-1]["sectionPlan"] and "breathing space" in " ".join(captured[-1]["sectionAndPhraseAdvice"])
    assert captured[-1]["timingAssumption"]["bpm"] == 84
    assert llm["lyrics"] == raw_lyrics
