"""Selectable musical guidance, frozen with each request rather than a quality verdict."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from .music3 import ENGINE

VERSION = 1
PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "emotional-hiphop", "label": "감성 힙합",
        "description": "느긋한 힙합 리듬과 반복되는 코드, 가까이 들리는 보컬을 중심으로 만들어요.",
        "caption": "emotional hip hop, laid-back boom bap, warm piano, mellow bass",
        "music3Caption": "Emotional melodic hip hop with wistful intimacy and quiet hope; warm piano and round bass over a laid-back boom-bap groove. Relaxed melodic-rap verses open into a tender fully sung chorus with a memorable rising-and-falling melody.",
        "music3InstrumentalCaption": "Emotional melodic hip hop with wistful intimacy and quiet hope; warm piano and round bass over a laid-back boom-bap groove. A recurring piano motif opens into a warmer melodic reprise.",
        "bpm": 84, "keyScale": "A minor", "timeSignature": "4",
        "ruleIds": ["steady-groove", "repeated-harmony", "focused-arrangement", "clear-vocal", "clean-production", "simple-structure"],
    },
    {
        "id": "upbeat-pop", "label": "신나는 팝",
        "description": "경쾌한 팝 리듬과 밝은 코드, 기억하기 쉬운 후렴을 중심으로 만들어요.",
        "caption": "upbeat pop, dance beat, bright keys, rhythmic bass",
        "music3Caption": "Bright, uplifting pop with a bouncy dance groove, bright keys and rhythmic bass. Playful rhythmic verses build anticipation; the chorus opens into a catchy singable melody with a clear melodic resolution.",
        "music3InstrumentalCaption": "Bright, uplifting pop with a bouncy dance groove, bright keys and rhythmic bass. A catchy recurring main theme opens into a fuller melodic reprise.",
        "bpm": 120, "keyScale": "C Major", "timeSignature": "4",
        "ruleIds": ["steady-groove", "repeated-harmony", "focused-arrangement", "clear-vocal", "clean-production", "simple-structure"],
    },
)
RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "steady-groove", "label": "리듬 일정하게",
        "description": "같은 템포와 드럼 패턴을 유지해 리듬이 흔들리지 않도록 유도해요.",
        "caption": "steady tempo, repeating drum groove, kick and bass locked",
        "music3Caption": "Steady tempo and a consistent drum pocket; kick and bass move together, with natural syncopated phrasing.",
    },
    {
        "id": "repeated-harmony", "label": "코드 반복하기",
        "description": "짧은 코드 진행과 하나의 조성을 유지하도록 유도해요.",
        "caption": "one key, repeating chord loop, recurring melodic motif",
        "music3Caption": "A cohesive repeating chord progression in one key, gentle voice leading and a recurring instrumental motif.",
    },
    {
        "id": "focused-arrangement", "label": "악기 수 줄이기",
        "description": "드럼·베이스·주요 악기를 중심으로 빈 공간이 있는 편곡을 유도해요.",
        "caption": "drums, bass and one main instrument, spacious arrangement",
        "music3Caption": "Spacious drums, bass and one main chord instrument; leave room for the lead melody.",
    },
    {
        "id": "clear-vocal", "label": "보컬 또렷하게",
        "description": "한 명의 보컬과 짧은 프레이즈로 가사가 잘 들리도록 유도해요. 연주곡에서는 적용하지 않아요.",
        "caption": "single clear lead vocal, short phrases, intelligible diction",
        "music3Caption": "One clear lead vocal, natural sung diction and short phrases shaped to the beat.",
    },
    {
        "id": "clean-production", "label": "깨끗한 음색",
        "description": "과한 질감 대신 선명한 음색과 균형 있는 믹스를 유도해요.",
        "caption": "clean studio sound, balanced mix, natural dynamics",
        "music3Caption": "Clean studio sound, warm full tone, balanced instrumental separation and natural dynamics.",
    },
    {
        "id": "simple-structure", "label": "구성 간단하게",
        "description": "45초 이하에서는 한 구절과 후렴 중심으로 간단하게 구성하고, 긴 곡은 같은 후렴을 반복하도록 유도해요.",
        "caption": "verse and recurring chorus, smooth transitions, resolved ending",
        "shortCaption": "one short verse, one memorable hook, brief resolved ending",
        "instrumentalCaption": "recurring main theme, smooth transitions, resolved ending",
        "shortInstrumentalCaption": "one recurring main theme, brief resolved ending",
        "music3Caption": "Coherent transitions between the supplied sections; each written chorus returns to the same recognizable melody, followed by a resolved ending.",
        "music3ShortCaption": "A short verse and memorable sung chorus, with a brief musically resolved ending.",
        "music3InstrumentalCaption": "A recurring main theme, coherent transitions and a musically resolved ending.",
        "music3ShortInstrumentalCaption": "A recognizable recurring main theme and a brief musically resolved ending.",
    },
    {
        "id": "section-development", "label": "구간별 전개 만들기",
        "description": "구절은 담백하게, 후렴은 힘 있게 전개하도록 구간별 편곡과 에너지를 안내해요. 직접 쓴 섹션 순서는 유지해요.",
        "caption": "restrained verses, building energy, fuller choruses, gentle ending",
        "shortCaption": "restrained verse, fuller hook, gentle ending",
        "instrumentalCaption": "restrained main theme, fuller reprise, gentle ending",
        "shortInstrumentalCaption": "restrained theme, fuller reprise, gentle ending",
        "music3Caption": "Restrained verses open into fuller choruses through melodic and harmonic lift; keep the groove continuous and let the ending settle gently.",
        "music3ShortCaption": "An intimate verse opens into a fuller melodic hook, then a brief gentle resolution.",
        "music3InstrumentalCaption": "A restrained theme opens into a fuller reprise through melodic and harmonic lift, then settles gently.",
        "music3ShortInstrumentalCaption": "A restrained theme, fuller melodic reprise and brief gentle resolution.",
    },
    {
        "id": "phrase-breathing", "label": "가사 호흡 나누기",
        "description": "단어와 순서를 보존하면서 긴 줄의 띄어쓰기·문장부호에서 호흡을 나누고, 템포에 맞는 여백을 안내해요.",
        "caption": "short singable phrases, rhythmic diction, breathing space between lines",
        "music3Caption": "Sing in relaxed, rhythmically connected phrases with natural breath between lines and room for sustained vowels.",
    },
    {
        "id": "melodic-hook", "label": "기억에 남는 선율",
        "description": "짧은 선율의 반복과 후렴의 상승·해소를 안내해 기억에 남는 멜로디를 유도해요.",
        "caption": "recognizable melodic hook, recurring sung motif, chorus lift and melodic resolution",
        "instrumentalCaption": "recognizable melodic theme, recurring motif, melodic lift and resolution",
        "music3Caption": "A distinctive short instrumental pickup previews the sung hook and establishes the groove. Across each written chorus, repeat a short melodic-and-rhythmic motif, answered by a gentle falling phrase; the final line resolves warmly.",
        "music3InstrumentalCaption": "A distinctive short opening theme establishes the groove. Return to its melodic-and-rhythmic motif in the fuller reprise, answered by a gentle falling phrase; the final phrase resolves warmly.",
    },
    {
        "id": "expressive-performance", "label": "보컬 감정 살리기",
        "description": "가사의 감정이 프레이징·음의 길이·강약으로 드러나도록 보컬 표현을 안내해요. 연주곡에서는 적용하지 않아요.",
        "caption": "emotionally connected lead vocal, shaped phrasing, intentional dynamics and sustained line endings",
        "music3Caption": "An emotionally connected lead performance: intimate verse phrasing opens into a warmer, more sustained chorus, with intentional dynamics and natural line endings.",
    },
)


def catalog(*, engine: str = ENGINE) -> dict[str, Any]:
    if engine not in {ENGINE, "ace-step"}:
        raise ValueError("unknown music engine")
    result = {"version": VERSION, "presets": deepcopy(list(PRESETS)), "rules": deepcopy(list(RULES)),
              "captionBudgetCharacters": 16000 if engine == ENGINE else 650}
    if engine == ENGINE:
        # This is the managed API's final prompt bound, not a tokenizer estimate.
        # Captions, structural instructions and headings all share that bound.
        result.update(captionBudgetScope="api_prompt_character_limit", captionTokenBudgetMeasured=False)
    return result


def normalize_selection(value: Any) -> dict[str, Any]:
    """Reject ambiguous/unknown controls; normalize order and duplicate checkbox IDs."""

    if not isinstance(value, dict) or set(value) != {"version", "presetId", "ruleIds"}:
        raise ValueError("production rules must contain version, presetId and ruleIds only")
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("unsupported production rules version")
    preset_id = value["presetId"]
    if preset_id is not None and (not isinstance(preset_id, str) or preset_id not in {p["id"] for p in PRESETS}):
        raise ValueError("unknown production preset")
    rule_ids = value["ruleIds"]
    if not isinstance(rule_ids, list) or any(not isinstance(item, str) for item in rule_ids):
        raise ValueError("production ruleIds must be a list of strings")
    known_ids = [rule["id"] for rule in RULES]
    if any(item not in known_ids for item in rule_ids):
        raise ValueError("unknown production rule")
    return {"version": VERSION, "presetId": preset_id, "ruleIds": [item for item in known_ids if item in rule_ids]}


def normalize_time_signature(value: Any) -> str | None:
    """ACE accepts meter numerators, including 6 for compound time."""

    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("time signature must be 2, 3, 4 or 6")
    text = str(value).strip()
    if not text:
        return None
    if text in {"2/4", "3/4", "4/4", "6/8"}:
        text = text.split("/")[0]
    if text not in {"2", "3", "4", "6"}:
        raise ValueError("time signature must be 2, 3, 4 or 6 (2/4, 3/4, 4/4 or 6/8)")
    return text


def freeze_guidance(
    selection: dict[str, Any], *, base_style: str, duration_seconds: float,
    instrumental: bool, input_metas: dict[str, Any] | None = None,
    previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Snapshot texts so a later catalog edit cannot alter source-version requests."""

    if previous_snapshot is None:
        selection = normalize_selection(selection)
        preset = next((deepcopy(p) for p in PRESETS if p["id"] == selection["presetId"]), None)
        rules = [deepcopy(rule) for rule in RULES if rule["id"] in selection["ruleIds"]]
    else:
        selection = deepcopy(previous_snapshot["selection"])
        preset = deepcopy(previous_snapshot["preset"])
        rules = deepcopy(previous_snapshot["rules"])
    captions = [preset["caption"]] if preset else []
    applied_ids: list[str] = []
    skipped_ids: list[str] = []
    for rule in rules:
        if instrumental and rule["id"] in {"clear-vocal", "phrase-breathing", "expressive-performance"}:
            skipped_ids.append(rule["id"])
            continue
        caption = rule["caption"]
        if instrumental:
            caption = rule.get("instrumentalCaption", caption)
        if duration_seconds <= 45:
            caption = rule.get("shortInstrumentalCaption" if instrumental else "shortCaption", caption)
        captions.append(caption)
        applied_ids.append(rule["id"])
    return {
        "version": previous_snapshot["version"] if previous_snapshot else VERSION,
        "selection": selection, "baseStylePrompt": base_style,
        "preset": preset, "rules": rules, "appliedCaptions": captions,
        "appliedRuleIds": applied_ids, "skippedRuleIds": skipped_ids,
        "inputMetas": deepcopy(input_metas or {}),
    }


def guided_prompt(base_style: str, snapshot: dict[str, Any]) -> str:
    # Feedback may carry an effective caption. Avoid repeating exact guidance;
    # leave manually entered musical descriptions intact.
    additions = [part for part in snapshot["appliedCaptions"] if part.casefold() not in base_style.casefold()]
    return ", ".join(part for part in [base_style, *additions] if part)


def draft_guidance(selection: dict[str, Any] | None, *, duration_seconds: float, instrumental: bool) -> dict[str, Any] | None:
    if selection is None:
        return None
    selection = normalize_selection(selection)
    if selection["presetId"] is None and not selection["ruleIds"]:
        return None
    return freeze_guidance(selection, base_style="", duration_seconds=duration_seconds, instrumental=instrumental)


def editable_style(style: str, original: dict[str, Any]) -> str:
    """Recover a source's base when feedback merely appends to its effective prompt."""

    snapshot = original.get("productionRules")
    if snapshot is None:
        return style
    if style == original["prompt"]:
        return snapshot["baseStylePrompt"]
    if style.startswith(original["prompt"] + ", "):
        return ", ".join(part for part in [snapshot["baseStylePrompt"], style[len(original["prompt"]) + 2:]] if part)
    return style
