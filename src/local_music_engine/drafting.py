"""Editable song drafts from a plain Korean description.

A configured writing LLM composes original lyrics. Music 3 itself has no writing
endpoint, so its offline fallback clearly identifies its example lyrics. Legacy
ACE sample drafts keep their language checks. Selected musical controls remain
separate from the base style and are frozen again when audio is generated.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Callable

from .ace_adapter import AceStepClient
from .assistant import AssistantError, LlmConfig, _chat
from .production_rules import draft_guidance, normalize_selection
from .song_planning import prepare_song_plan
from .music3 import BASE_URL, ENGINE, MAX_DURATION_SECONDS
from .lyrics import is_instrumental_lyrics

INSTRUMENTAL = "[Instrumental]"

# Korean descriptions become English musical tags, with genre before mood.
KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"발라드", "Korean ballad"),
    (r"인디", "indie pop"),
    (r"케이\s*팝|k-?pop|아이돌", "K-pop"),
    (r"댄스", "dance"),
    (r"시티\s*팝", "city pop"),
    (r"어쿠스틱", "acoustic"),
    (r"포크", "folk"),
    (r"알앤비|r&b", "R&B"),
    (r"록|락|밴드", "band rock"),
    (r"로파이|lo-?fi", "lo-fi"),
    (r"재즈", "jazz"),
    (r"힙합|랩", "hip hop"),
    (r"싱잉\s*랩|멜로딕\s*랩|노래하듯.*랩", "melodic rap"),
    (r"일렉|edm|전자", "electronic"),
    (r"트로트", "trot"),
    (r"영화|시네마틱|웅장", "cinematic"),
    (r"동요", "children's song"),
    (r"캐롤|크리스마스", "christmas song"),
    (r"잔잔|차분|조용", "calm"),
    (r"따뜻|포근", "warm"),
    (r"감성|감정.*담|감정.*살", "emotional"),
    (r"멜로디|선율", "melodic"),
    (r"몽환", "dreamy"),
    (r"신나|신날|흥겨", "energetic"),
    (r"밝은|밝게|경쾌", "bright"),
    (r"희망|벅찬", "uplifting"),
    (r"슬픈|슬프|애절|우울|눈물", "melancholic"),
    (r"설레|사랑", "romantic"),
    (r"그리운|그리움|추억|옛날", "nostalgic"),
    (r"쓸쓸|외로|혼자", "lonely"),
    (r"비\s*오|빗소리|장마|비가", "rainy mood"),
    (r"밤", "night"),
    (r"새벽", "dawn"),
    (r"여름", "summer"),
    (r"겨울|눈 오", "winter"),
    (r"봄", "spring"),
    (r"가을", "autumn"),
    (r"바다|파도", "ocean"),
    (r"드라이브", "driving"),
    (r"카페", "cafe"),
    (r"졸업|응원", "anthem"),
    (r"여(자|성)\s*(목소리|보컬|가수)", "female vocal"),
    (r"남(자|성)\s*(목소리|보컬|가수)", "male vocal"),
    (r"떼창|합창|같이 부르", "group vocals"),
    (r"속삭", "breathy vocal"),
    (r"피아노", "piano"),
    (r"어쿠스틱\s*기타|통기타", "acoustic guitar"),
    (r"일렉\s*기타|전자\s*기타", "electric guitar"),
    (r"현악|스트링|바이올린|첼로", "strings"),
    (r"신스|신디", "synth"),
    (r"느린|느리게|천천히", "slow tempo"),
    (r"빠른|빠르게", "upbeat"),
)

HANGUL = re.compile(r"[가-힣]")
LANGUAGE_MARKER = re.compile(r"\[(ko|en|ja|zh)\]\s*", re.IGNORECASE)
KOREAN_MARKER = re.compile(r"^\s*\[ko\]", re.IGNORECASE)
SECTION_TAG = re.compile(r"\s*\[[^\]]*\]\s*")


def keyword_tags(description: str) -> list[str]:
    text = description.lower()
    tags: list[str] = []
    for pattern, tag in KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE) and tag not in tags:
            tags.append(tag)
    return tags


def usable_korean_lyrics(lyrics: str) -> str | None:
    """Keep lyrics whose Korean is written in Hangul; English lines may sit beside it.

    A bilingual song (Korean verse, English hook) is a normal request, so English
    lines are not counted against the draft. Romanized Korean is what must not reach
    the engine: a line ACE itself marks ``[ko]`` has to contain Hangul, and lyrics
    without any Hangul line are rejected.
    """

    lines = [line for line in (lyrics or "").splitlines() if line.strip() and not SECTION_TAG.fullmatch(line)]
    if not lines:
        return None
    if any(KOREAN_MARKER.match(line) and not HANGUL.search(line) for line in lines):
        return None
    if not any(HANGUL.search(line) for line in lines):
        return None
    return LANGUAGE_MARKER.sub("", lyrics).strip()


def usable_english_lyrics(lyrics: str) -> str | None:
    lines = [line for line in (lyrics or "").splitlines() if line.strip() and not SECTION_TAG.fullmatch(line)]
    if not lines or any(KOREAN_MARKER.match(line) or HANGUL.search(line) for line in lines):
        return None
    if not any(re.search(r"[A-Za-z]", line) for line in lines):
        return None
    return LANGUAGE_MARKER.sub("", lyrics).strip()


def _vocal_language(value: str) -> str:
    if value not in {"ko", "en"}:
        raise ValueError("draft vocal language must be ko or en")
    return value


def _draft_controls(result: dict[str, Any], guidance: dict[str, Any] | None, duration_seconds: float, vocal_language: str) -> dict[str, Any]:
    result["vocalLanguage"] = vocal_language
    if guidance is not None:
        result["productionRules"] = guidance["selection"]
        result["durationSeconds"] = float(duration_seconds)
        if guidance["preset"]:
            for field in ("bpm", "keyScale", "timeSignature"):
                result[field] = guidance["preset"][field]
        result["notes"].append("제작 규칙을 초안 요청에 반영했어요. 최종 생성에도 선택한 규칙을 함께 전달해요.")
        if {"section-development", "phrase-breathing"}.intersection(guidance["appliedRuleIds"]):
            result["songPlan"] = _planning_advice(guidance, duration_seconds, is_instrumental_lyrics(result["lyrics"]), lyrics=result["lyrics"])
    return result


def _planning_advice(guidance: dict[str, Any], duration_seconds: float, instrumental: bool, *, lyrics: str = "") -> dict[str, Any]:
    preset = guidance["preset"] or {}
    return prepare_song_plan(
        lyrics, duration_seconds=duration_seconds, bpm=preset.get("bpm"), time_signature=preset.get("timeSignature"),
        preset_id=guidance["selection"]["presetId"], instrumental=instrumental,
        development="section-development" in guidance["appliedRuleIds"], breathing="phrase-breathing" in guidance["appliedRuleIds"],
    )


def _result(**values: Any) -> dict[str, Any]:
    base = {
        "title": None,
        "stylePrompt": "",
        "lyrics": "",
        "durationSeconds": None,
        "bpm": None,
        "keyScale": None,
        "timeSignature": None,
        "source": "engine",
        "sourceModel": None,
        "notes": [],
    }
    base.update(values)
    return base


def engine_draft(
    description: str,
    *,
    instrumental: bool,
    base_url: str,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
    duration_seconds: float = 120.0,
    production_rules: dict[str, Any] | None = None,
    vocal_language: str = "ko",
    engine: str = ENGINE,
) -> dict[str, Any]:
    if engine == ENGINE:
        return rules_draft(description, instrumental=instrumental, duration_seconds=duration_seconds,
                           production_rules=production_rules, vocal_language=vocal_language)
    if engine != "ace-step":
        raise ValueError("unknown music engine")
    if base_url == BASE_URL:
        base_url = "http://127.0.0.1:18001"
    vocal_language = _vocal_language(vocal_language)
    guidance = draft_guidance(production_rules, duration_seconds=duration_seconds, instrumental=instrumental)
    tags = keyword_tags(description)
    notes: list[str] = []
    if instrumental and "instrumental" not in tags:
        tags.append("instrumental")
    if tags:
        query = ", ".join(tags)
    else:
        query = description
        notes.append(
            "설명에서 아는 단어를 찾지 못해 원문 그대로 엔진에 물었어요. "
            "엔진은 한국어 설명을 잘 알아듣지 못하니 스타일을 꼭 확인하세요."
        )
    if guidance is not None:
        query = ", ".join([query, *guidance["appliedCaptions"], f"{duration_seconds:g}-second song"])
        if "simple-structure" in guidance["appliedRuleIds"] and not instrumental and duration_seconds <= 45:
            query += ", lyrics with one short verse and one hook, 4 to 6 short lines total"
        if {"section-development", "phrase-breathing"}.intersection(guidance["appliedRuleIds"]):
            query += ", " + " ".join(_planning_advice(guidance, duration_seconds, instrumental)["guidance"])
    data = client_factory(base_url).create_sample(
        query, vocal_language=vocal_language, instrumental=instrumental
    )
    caption = " ".join(str(data.get("caption") or "").split()) or ", ".join(tags)
    lyric_validator = usable_korean_lyrics if vocal_language == "ko" else usable_english_lyrics
    lyrics = INSTRUMENTAL if instrumental else lyric_validator(str(data.get("lyrics") or ""))
    if lyrics is None:
        lyrics = ""
        notes.append("엔진이 쓴 가사가 한글이 아니라서 비워 뒀어요. 가사를 직접 쓰거나, 설정에서 로컬 LLM 도우미를 켜면 한글 가사까지 써 줘요."
                     if vocal_language == "ko" else "엔진이 쓴 가사가 영어가 아니라서 비워 뒀어요. 영어 가사를 직접 쓰거나 로컬 LLM 도우미를 이용해 주세요.")
    duration = data.get("duration")
    result = _result(
        stylePrompt=caption,
        lyrics=lyrics,
        durationSeconds=float(duration) if isinstance(duration, (int, float)) and 10 <= duration <= 600 else None,
        source="engine",
        sourceModel="ACE-Step 5Hz LM",
        notes=notes,
        understoodTags=tags,
    )
    return _draft_controls(result, guidance, duration_seconds, vocal_language)


def rules_draft(description: str, *, instrumental: bool, duration_seconds: float,
                production_rules: dict[str, Any] | None = None, vocal_language: str = "ko") -> dict[str, Any]:
    """A transparent editable starting point when no writing assistant is configured."""
    if not description.strip():
        raise ValueError("describe the song before asking for a draft")
    if not math.isfinite(duration_seconds) or not 10 <= duration_seconds <= MAX_DURATION_SECONDS:
        raise ValueError("Music 3 duration must be between 10 and 300 seconds")
    vocal_language = _vocal_language(vocal_language)
    guidance = draft_guidance(production_rules, duration_seconds=duration_seconds, instrumental=instrumental)
    tags = keyword_tags(description)
    bright = any(tag in tags for tag in ("energetic", "bright", "uplifting", "dance"))
    if vocal_language == "en":
        verse = ["Morning light is calling", "I can feel the day begin", "Every step is lighter", "Let the sunlight in"] if bright else ["City lights are fading", "Your voice stays with me", "I walk through the silence", "And let the night breathe"]
        chorus = ["We can rise together", "Let the whole world know", "Keep this feeling with us", "Everywhere we go"] if bright else ["Stay until the morning", "Let the cold wind go", "Step into the daylight", "We can take it slow"]
        verse2 = ["Every color finds us", "With every step we take", "Your smile becomes the rhythm", "A brighter day awaits"] if bright else ["Leave the hurt behind us", "With every step we take", "Your hand is warm in mine", "The sky begins to change"]
    else:
        verse = ["아침빛이 번져 와", "발걸음이 가벼워", "너와 같은 길 위에", "새로운 하루를 열어"] if bright else ["가로등이 흐려져", "네 목소린 남아 있어", "조용한 길을 걸어", "밤이 천천히 숨 쉬어"]
        chorus = ["우리 함께 달려가", "이 순간을 기억해", "너와 나의 멜로디", "어디라도 이어져"] if bright else ["아침까지 곁에 있어", "차가운 바람은 보내", "햇살 속으로 걸어가", "우리 천천히 가도 돼"]
        verse2 = ["우리 발을 맞춰 봐", "거리에 빛이 번져", "네 웃음이 박자가 돼", "새로운 길이 열려"] if bright else ["아픈 날은 뒤로 두고", "걸음마다 가벼워져", "네 손의 온길 따라서", "하늘빛이 달라져"]
    if instrumental:
        lyrics = INSTRUMENTAL
    else:
        if duration_seconds <= 45:
            verse, chorus = verse[:2], chorus[:2]
        sections = ["[Verse]", *verse, "", "[Chorus]", *chorus]
        if duration_seconds >= 80:
            sections.extend(["", "[Verse 2]", *verse2, "", "[Chorus]", *chorus])
        sections.extend(["", "[Outro]"])
        lyrics = "\n".join(sections)
    result = _result(title="새로운 하루" if bright else "새벽의 걸음", stylePrompt=", ".join(tags) or description.strip(),
                     lyrics=lyrics, durationSeconds=float(duration_seconds), source="rules", sourceModel=None,
                     understoodTags=tags, notes=["선택한 분위기에 맞춘 내장 규칙 초안이에요. 가사는 예시이므로 원하는 이야기로 고쳐 주세요. 로컬 LLM 도우미를 설정하면 설명을 바탕으로 새 가사를 쓸 수 있어요."])
    return _draft_controls(result, guidance, duration_seconds, vocal_language)


DRAFT_PROMPT = """You write the first draft of a song for MiniMax Music 3, a local music model, from a
plain Korean description by someone with no musical training.

Return JSON with:
- "title": a short Korean title (at most 20 characters).
- "caption": English, comma-separated musical style tags: genre, mood, vocal type,
  main instruments, tempo feel, production. 8 to 16 tags. Only what the description asks for or
  clearly implies; do not add unrelated genres. If a melodic hook or expressive performance
  is selected, describe its musical contour or vocal delivery concisely; keep the main
  emotional character consistent across the verse and chorus.
- "lyrics": singable lyrics in requestedVocalLanguage, with section tags on their own lines.
  For ko, write Korean in Hangul, never romanized; English hooks requested by the description
  are welcome. For en, write natural English lyrics. Use short lines (about 6-12 syllables),
  roughly one line per 4 seconds. For songs up to 45 seconds, use only a short [Verse] and
  one [Chorus], 4 to 6 short lines total; let the music provide the brief ending. For longer
  songs, use verses and a repeating chorus. If instrumental, exactly "[Instrumental]".
- "durationSeconds": the length you would suggest, between 30 and 240.
Return JSON only."""

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "lyrics": {"type": "string"},
        "durationSeconds": {"type": "number"},
    },
    "required": ["title", "caption", "lyrics"],
}


def draft_from_llm_answer(raw: str, *, instrumental: bool, model: str, vocal_language: str = "ko") -> dict[str, Any]:
    vocal_language = _vocal_language(vocal_language)
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError as error:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise AssistantError("LLM draft is not JSON") from error
        answer = json.loads(match.group(0))
    if not isinstance(answer, dict):
        raise AssistantError("LLM draft is not a JSON object")
    caption = " ".join(str(answer.get("caption") or "").split())
    if not caption or len(caption) > 1500:
        raise AssistantError("LLM caption is empty or too long")
    # Models sometimes double-escape newlines inside the JSON string.
    lyrics = str(answer.get("lyrics") or "").replace("\\n", "\n").strip()
    notes: list[str] = []
    if instrumental:
        lyrics = INSTRUMENTAL
    else:
        usable = (usable_korean_lyrics if vocal_language == "ko" else usable_english_lyrics)(lyrics)
        if usable is None or len(usable) > 4096:
            raise AssistantError("LLM lyrics have no Korean in Hangul (romanized?) or are too long" if vocal_language == "ko"
                                 else "LLM lyrics are not English or are too long")
        lyrics = usable
    title = " ".join(str(answer.get("title") or "").split())[:40] or None
    duration = answer.get("durationSeconds")
    return _result(
        title=title,
        stylePrompt=caption,
        lyrics=lyrics,
        durationSeconds=float(duration) if isinstance(duration, (int, float)) and 10 <= duration <= 600 else None,
        source="llm",
        sourceModel=model,
        notes=notes,
    )


def llm_draft(description: str, *, instrumental: bool, duration_seconds: float, config: LlmConfig,
              production_rules: dict[str, Any] | None = None, vocal_language: str = "ko") -> dict[str, Any]:
    vocal_language = _vocal_language(vocal_language)
    guidance = draft_guidance(production_rules, duration_seconds=duration_seconds, instrumental=instrumental)
    config = config.validated()
    request = {
        "description": description,
        "instrumental": instrumental,
        "requestedLengthSeconds": round(duration_seconds),
        "requestedVocalLanguage": vocal_language,
    }
    if guidance is not None:
        request["productionGuidance"] = {"captions": guidance["appliedCaptions"], "preset": guidance["preset"]}
        request["captionInstruction"] = "Return the base musical style only; selected production guidance will be added separately during generation."
        if {"section-development", "phrase-breathing"}.intersection(guidance["appliedRuleIds"]):
            planning = _planning_advice(guidance, duration_seconds, instrumental)
            request["sectionAndPhraseAdvice"] = planning["guidance"]
            request["sectionPlan"] = [{key: section[key] for key in ("label", "bars", "energy", "instruments")} for section in planning["arrangement"]]
            request["timingAssumption"] = planning["timing"]
    raw = _chat(
        config,
        [
            {"role": "system", "content": DRAFT_PROMPT},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ],
        schema=DRAFT_SCHEMA,
    )
    result = draft_from_llm_answer(raw, instrumental=instrumental, model=config.model, vocal_language=vocal_language)
    return _draft_controls(result, guidance, duration_seconds, vocal_language)


def draft_song(
    description: str,
    *,
    instrumental: bool,
    duration_seconds: float,
    base_url: str,
    llm: LlmConfig | None = None,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
    production_rules: dict[str, Any] | None = None,
    vocal_language: str = "ko",
    engine: str = ENGINE,
) -> dict[str, Any]:
    if not description.strip():
        raise ValueError("describe the song before asking for a draft")
    maximum = MAX_DURATION_SECONDS if engine == ENGINE else 600
    if not math.isfinite(duration_seconds) or not 10 <= duration_seconds <= maximum:
        raise ValueError(f"duration must be between 10 and {maximum} seconds")
    vocal_language = _vocal_language(vocal_language)
    if production_rules is not None:
        production_rules = normalize_selection(production_rules)
    if llm is not None:
        try:
            return llm_draft(description, instrumental=instrumental, duration_seconds=duration_seconds, config=llm,
                             production_rules=production_rules, vocal_language=vocal_language)
        except (AssistantError, ValueError) as error:
            try:
                result = engine_draft(description, instrumental=instrumental, base_url=base_url, client_factory=client_factory,
                                      duration_seconds=duration_seconds, production_rules=production_rules, vocal_language=vocal_language, engine=engine)
            except Exception as engine_error:
                # The fallback's own failure (often "engine is off") must not hide why
                # the LLM draft was refused in the first place.
                raise AssistantError(
                    f"LLM draft failed ({error}); engine draft also failed ({type(engine_error).__name__}: {engine_error})"
                ) from engine_error
            result["notes"].insert(0, f"LLM 도우미를 쓰지 못해 {'내장 규칙' if engine == ENGINE else '음악 엔진'}으로 초안을 만들었어요 ({error}).")
            return result
    return engine_draft(description, instrumental=instrumental, base_url=base_url, client_factory=client_factory,
                        duration_seconds=duration_seconds, production_rules=production_rules, vocal_language=vocal_language, engine=engine)
