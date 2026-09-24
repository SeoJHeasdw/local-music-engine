"""First drafts of a song (style caption, lyrics, title) from a plain Korean description.

Measured on 2026-09-22 with ACE-Step 1.5's 0.6B LM (`/v1/create_sample`): a Korean
description came back as an unrelated genre, and lyrics came back romanized
(`[ko] nega iss-eo`). The same request in English produced a fitting caption. So:

- with a loopback LLM configured, the LLM writes the caption and Hangul lyrics;
- otherwise known Korean words are mapped to English tags, ACE is asked in English, and
  lyrics that are not Hangul are dropped and reported instead of shown as a draft.

Guessed BPM/key are not returned; ACE fills missing metas itself while generating.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .ace_adapter import AceStepClient
from .assistant import AssistantError, LlmConfig, _chat

INSTRUMENTAL = "[Instrumental]"

# Korean words a listener uses → tags ACE understands. Order is the order tags are
# written, so genre comes first the way ACE captions usually start.
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
    (r"일렉|edm|전자", "electronic"),
    (r"트로트", "trot"),
    (r"영화|시네마틱|웅장", "cinematic"),
    (r"동요", "children's song"),
    (r"캐롤|크리스마스", "christmas song"),
    (r"잔잔|차분|조용", "calm"),
    (r"따뜻|포근", "warm"),
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
) -> dict[str, Any]:
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
    data = client_factory(base_url).create_sample(
        query, vocal_language="ko", instrumental=instrumental
    )
    caption = " ".join(str(data.get("caption") or "").split()) or ", ".join(tags)
    lyrics = INSTRUMENTAL if instrumental else usable_korean_lyrics(str(data.get("lyrics") or ""))
    if lyrics is None:
        lyrics = ""
        notes.append(
            "엔진이 쓴 가사가 한글이 아니라서 비워 뒀어요. 가사를 직접 쓰거나, "
            "설정에서 로컬 LLM 도우미를 켜면 한글 가사까지 써 줘요."
        )
    duration = data.get("duration")
    return _result(
        stylePrompt=caption,
        lyrics=lyrics,
        durationSeconds=float(duration) if isinstance(duration, (int, float)) and 10 <= duration <= 600 else None,
        source="engine",
        sourceModel="ACE-Step 5Hz LM",
        notes=notes,
        understoodTags=tags,
    )


DRAFT_PROMPT = """You write the first draft of a song for ACE-Step 1.5, a local music model, from a
plain Korean description by someone with no musical training.

Return JSON with:
- "title": a short Korean title (at most 20 characters).
- "caption": English, comma-separated style tags ACE-Step understands: genre, mood, vocal type,
  main instruments, tempo feel, production. 8 to 16 tags. Only what the description asks for or
  clearly implies; do not add unrelated genres.
- "lyrics": singable lyrics with section tags on their own lines: [Verse], [Pre-Chorus], [Chorus],
  [Bridge], [Outro]. Korean is the main language and is always written in Hangul, never
  romanized. If the description asks for English (for example an English hook or chorus), write
  those lines in natural English. Short lines (about 6-12 syllables). Repeat the chorus. Roughly
  one line per 4 seconds of the requested length. If instrumental, exactly "[Instrumental]".
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


def draft_from_llm_answer(raw: str, *, instrumental: bool, model: str) -> dict[str, Any]:
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
        usable = usable_korean_lyrics(lyrics)
        if usable is None or len(usable) > 4096:
            raise AssistantError("LLM lyrics have no Korean in Hangul (romanized?) or are too long")
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


def llm_draft(description: str, *, instrumental: bool, duration_seconds: float, config: LlmConfig) -> dict[str, Any]:
    config = config.validated()
    request = {
        "description": description,
        "instrumental": instrumental,
        "requestedLengthSeconds": round(duration_seconds),
    }
    raw = _chat(
        config,
        [
            {"role": "system", "content": DRAFT_PROMPT},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ],
        schema=DRAFT_SCHEMA,
    )
    return draft_from_llm_answer(raw, instrumental=instrumental, model=config.model)


def draft_song(
    description: str,
    *,
    instrumental: bool,
    duration_seconds: float,
    base_url: str,
    llm: LlmConfig | None = None,
    client_factory: Callable[..., AceStepClient] = AceStepClient,
) -> dict[str, Any]:
    if not description.strip():
        raise ValueError("describe the song before asking for a draft")
    if llm is not None:
        try:
            return llm_draft(description, instrumental=instrumental, duration_seconds=duration_seconds, config=llm)
        except (AssistantError, ValueError) as error:
            try:
                result = engine_draft(description, instrumental=instrumental, base_url=base_url, client_factory=client_factory)
            except Exception as engine_error:
                # The fallback's own failure (often "engine is off") must not hide why
                # the LLM draft was refused in the first place.
                raise AssistantError(
                    f"LLM draft failed ({error}); engine draft also failed ({type(engine_error).__name__}: {engine_error})"
                ) from engine_error
            result["notes"].insert(0, f"LLM 도우미를 쓰지 못해 음악 엔진으로 초안을 만들었어요 ({error}).")
            return result
    return engine_draft(description, instrumental=instrumental, base_url=base_url, client_factory=client_factory)
