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
        "music3Schema": {
            "genre": "Hip-Hop / Melodic Rap",
            "emotion": "The track opens directly into an intimate, wistful late-night mood, with the vocal entering early over the piano loop in a relaxed, reflective verse. Moving into the chorus, the emotion warms and lifts into a tender, fully sung hook that carries a quiet sense of hope, before settling into a soft, resolved ending.",
            "scenario": "Late-night walks under city lights, quiet drives home after a long day, and solitary moments of reflection by a window at night.",
            "sonics": "The production is clean, warm and polished with a modern soundstage; the low end is round and controlled, the lead vocal sits close and forward in the center, and the dynamics breathe naturally between the restrained verse and the fuller chorus.",
            "timbre": "a soft, warm timbre with an intimate, slightly breathy texture and clear articulation",
            "style": "The verse is delivered in a laid-back, melodic rap-sing flow with clear diction and short phrases locked to the beat. The chorus shifts into a fully sung, memorable melody with a gentle rise and fall and sustained line endings.",
            "harmony": "Subtle doubled vocals and soft harmonies thicken the chorus, supporting the hook without crowding the lead.",
            "fx": "The lead vocal has moderate compression with a touch of plate reverb and a short delay; the verse stays relatively dry for clarity while the chorus opens into a wider space.",
            "primary": "A warm acoustic piano plays a repeating melancholic chord loop with a recurring melodic motif, present from the first bar through the final chord.",
            "secondary": "A round, mellow bass locks with the kick drum throughout. Soft atmospheric pads swell gently into the chorus to widen the space and recede for the ending.",
            "groove": "A laid-back boom-bap drum groove with a soft punchy kick, crisp snare and lightly swung hi-hats keeps a steady head-nodding pulse from start to finish. The verse groove is stripped back to leave room for the vocal, and the chorus adds open hi-hats and a fuller kick pattern for lift.",
            "textures": "A subtle reverse swell marks the transition into the chorus, and the final phrase rings out over the piano for a clean, resolved ending.",
        },
        "music3InstrumentalSchema": {
            "genre": "Hip-Hop / Instrumental Boom Bap",
            "emotion": "The track opens directly into an intimate, wistful late-night mood with the piano motif in front. In the fuller reprise the theme warms and lifts with a quiet sense of hope, before settling into a soft, resolved ending.",
            "scenario": "Late-night walks under city lights, quiet drives home after a long day, and solitary moments of reflection by a window at night.",
            "sonics": "The production is clean, warm and polished with a modern soundstage; the low end is round and controlled, the piano sits close and forward, and the dynamics breathe naturally between the restrained opening and the fuller reprise.",
            "lead": "A warm acoustic piano carries the lead melody.",
            "style": "The piano melody is phrased like a relaxed vocal line, with short motifs and space between phrases.",
            "harmony": "Soft pads and a gentle countermelody answer the piano motif in the fuller reprise.",
            "fx": "There are no vocal effects; the piano has a gentle room reverb and a short delay on phrase endings.",
            "primary": "A warm acoustic piano plays a repeating melancholic chord loop and the recurring main motif, present from the first bar through the final chord.",
            "secondary": "A round, mellow bass locks with the kick drum throughout. Soft atmospheric pads swell gently into the reprise and recede for the ending.",
            "groove": "A laid-back boom-bap drum groove with a soft punchy kick, crisp snare and lightly swung hi-hats keeps a steady head-nodding pulse. The opening groove is stripped back, and the reprise adds open hi-hats and a fuller kick pattern for lift.",
            "textures": "A subtle reverse swell marks the move into the reprise, and the last motif rings out over the piano for a clean, resolved ending.",
        },
        "bpm": 84, "keyScale": "A minor", "timeSignature": "4",
        "ruleIds": ["steady-groove", "repeated-harmony", "focused-arrangement", "clear-vocal", "clean-production", "simple-structure"],
    },
    {
        "id": "upbeat-pop", "label": "신나는 팝",
        "description": "경쾌한 팝 리듬과 밝은 코드, 기억하기 쉬운 후렴을 중심으로 만들어요.",
        "caption": "upbeat pop, dance beat, bright keys, rhythmic bass",
        "music3Schema": {
            "genre": "Pop / Dance-Pop",
            "emotion": "The track opens with bright, buoyant energy, with the vocal entering early over a bouncy groove in a playful, rhythmic verse that builds anticipation. The chorus opens into a catchy, uplifting sing-along melody with a clear sense of release, and the song ends on a confident, resolved note.",
            "scenario": "Sunny drives with the windows down, getting ready for a night out, and carefree moments with friends.",
            "sonics": "The production is bright, punchy and polished with a wide modern soundstage; the kick and bass are tight and forward, the lead vocal sits clearly on top, and the chorus feels noticeably bigger than the verse.",
            "timbre": "a bright, clear timbre with an energetic, youthful tone",
            "style": "The verse is delivered with crisp, rhythmic phrasing that bounces on the beat. The chorus opens into a catchy, singable melody with a clear rise and a satisfying melodic resolution.",
            "harmony": "Stacked vocal doubles and bright harmonies widen the chorus, with short call-and-response backing phrases.",
            "fx": "The lead vocal is polished with moderate compression, a bright plate reverb and tempo-synced delay throws at the ends of chorus lines.",
            "primary": "Bright, punchy keys play a repeating chord pattern with a catchy recurring riff from the first bar to the end.",
            "secondary": "A rhythmic, bouncing bass line drives the groove alongside the kick. Shimmering synth layers enter in the chorus to lift the energy and drop back for the verse.",
            "groove": "A bouncy four-on-the-floor dance beat with a tight kick, crisp claps on beats two and four and syncopated hi-hats keeps the energy moving throughout. The verse groove is lighter, and the chorus adds fuller percussion and open hi-hats.",
            "textures": "Short risers and filtered sweeps lead into each chorus, and the final chorus resolves cleanly on the main riff.",
        },
        "music3InstrumentalSchema": {
            "genre": "Pop / Instrumental Dance-Pop",
            "emotion": "The track opens with bright, buoyant energy and a playful main riff that builds anticipation. The fuller reprise opens into an uplifting melodic lift with a clear sense of release, and the piece ends on a confident, resolved note.",
            "scenario": "Sunny drives with the windows down, getting ready for a night out, and carefree moments with friends.",
            "sonics": "The production is bright, punchy and polished with a wide modern soundstage; the kick and bass are tight and forward, the lead synth sits clearly on top, and the reprise feels noticeably bigger than the opening.",
            "lead": "A bright plucked synth carries the lead melody.",
            "style": "The lead melody uses crisp, rhythmic phrasing that bounces on the beat and opens into a catchy, singable line in the reprise.",
            "harmony": "Layered synth octaves and bright chords widen the reprise, with short call-and-response riffs.",
            "fx": "There are no vocal effects; the lead synth has a bright plate reverb and tempo-synced delay throws.",
            "primary": "Bright, punchy keys play a repeating chord pattern with a catchy recurring riff from the first bar to the end.",
            "secondary": "A rhythmic, bouncing bass line drives the groove alongside the kick. Shimmering synth layers enter in the reprise to lift the energy.",
            "groove": "A bouncy four-on-the-floor dance beat with a tight kick, crisp claps on beats two and four and syncopated hi-hats keeps the energy moving throughout. The opening groove is lighter, and the reprise adds fuller percussion and open hi-hats.",
            "textures": "Short risers and filtered sweeps lead into the reprise, and the ending resolves cleanly on the main riff.",
        },
        "bpm": 120, "keyScale": "C Major", "timeSignature": "4",
        "ruleIds": ["steady-groove", "repeated-harmony", "focused-arrangement", "clear-vocal", "clean-production", "simple-structure"],
    },
)
RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "steady-groove", "label": "리듬 일정하게",
        "description": "같은 템포와 드럼 패턴을 유지해 리듬이 흔들리지 않도록 유도해요.",
        "caption": "steady tempo, repeating drum groove, kick and bass locked",
        "music3Fields": {"groove": "The tempo stays steady from start to finish, with a consistent drum pocket in which the kick and bass move together under naturally syncopated phrasing."},
    },
    {
        "id": "repeated-harmony", "label": "코드 반복하기",
        "description": "짧은 코드 진행과 하나의 조성을 유지하도록 유도해요.",
        "caption": "one key, repeating chord loop, recurring melodic motif",
        "music3Fields": {"primary": "The harmony stays in one key on a cohesive, repeating chord progression with gentle voice leading and a recurring instrumental motif."},
    },
    {
        "id": "focused-arrangement", "label": "악기 수 줄이기",
        "description": "드럼·베이스·주요 악기를 중심으로 빈 공간이 있는 편곡을 유도해요.",
        "caption": "drums, bass and one main instrument, spacious arrangement",
        "music3Fields": {"secondary": "The arrangement stays spacious, built mainly on drums, bass and one main chord instrument, so the lead melody always has room."},
    },
    {
        "id": "clear-vocal", "label": "보컬 또렷하게",
        "description": "한 명의 보컬과 짧은 프레이즈로 가사가 잘 들리도록 유도해요. 연주곡에서는 적용하지 않아요.",
        "caption": "single clear lead vocal, short phrases, intelligible diction",
        "music3Fields": {"style": "A single clear lead vocal carries the song with natural, intelligible diction and short phrases shaped to the beat."},
    },
    {
        "id": "clean-production", "label": "깨끗한 음색",
        "description": "과한 질감 대신 선명한 음색과 균형 있는 믹스를 유도해요.",
        "caption": "clean studio sound, balanced mix, natural dynamics",
        "music3Fields": {"sonics": "The mix is clean and warm, with a full tone, balanced instrumental separation and natural dynamics."},
    },
    {
        "id": "simple-structure", "label": "구성 간단하게",
        "description": "45초 이하에서는 한 구절과 후렴 중심으로 간단하게 구성하고, 긴 곡은 같은 후렴을 반복하도록 유도해요.",
        "caption": "verse and recurring chorus, smooth transitions, resolved ending",
        "shortCaption": "one short verse, one memorable hook, brief resolved ending",
        "instrumentalCaption": "recurring main theme, smooth transitions, resolved ending",
        "shortInstrumentalCaption": "one recurring main theme, brief resolved ending",
        "music3Fields": {"emotion": "Each written chorus returns to the same recognizable melody through smooth transitions, and the song closes with a musically resolved ending."},
        "music3ShortFields": {"emotion": "A short verse leads into one memorable sung chorus and a brief, musically resolved ending."},
        "music3InstrumentalFields": {"emotion": "A recurring main theme returns through smooth transitions and closes with a musically resolved ending."},
        "music3ShortInstrumentalFields": {"emotion": "One recognizable main theme returns briefly before a musically resolved ending."},
    },
    {
        "id": "section-development", "label": "구간별 전개 만들기",
        "description": "구절은 담백하게, 후렴은 힘 있게 전개하도록 구간별 편곡과 에너지를 안내해요. 직접 쓴 섹션 순서는 유지해요.",
        "caption": "restrained verses, building energy, fuller choruses, gentle ending",
        "shortCaption": "restrained verse, fuller hook, gentle ending",
        "instrumentalCaption": "restrained main theme, fuller reprise, gentle ending",
        "shortInstrumentalCaption": "restrained theme, fuller reprise, gentle ending",
        "music3Fields": {"groove": "The verses are restrained and the choruses open up through melodic and harmonic lift and fuller percussion, while the groove stays continuous and the ending settles gently."},
        "music3ShortFields": {"groove": "The intimate verse opens into a fuller melodic hook, then the groove settles into a brief, gentle resolution."},
        "music3InstrumentalFields": {"groove": "A restrained statement of the theme opens into a fuller reprise through melodic and harmonic lift, then settles gently."},
        "music3ShortInstrumentalFields": {"groove": "A restrained theme opens into a fuller melodic reprise and a brief, gentle resolution."},
    },
    {
        "id": "phrase-breathing", "label": "가사 호흡 나누기",
        "description": "단어와 순서를 보존하면서 긴 줄의 띄어쓰기·문장부호에서 호흡을 나누고, 템포에 맞는 여백을 안내해요.",
        "caption": "short singable phrases, rhythmic diction, breathing space between lines",
        "music3Fields": {"style": "Phrases are relaxed and rhythmically connected, with natural breaths between lines and room for sustained vowels."},
    },
    {
        "id": "melodic-hook", "label": "기억에 남는 선율",
        "description": "짧은 선율의 반복과 후렴의 상승·해소를 안내해 기억에 남는 멜로디를 유도해요.",
        "caption": "recognizable melodic hook, recurring sung motif, chorus lift and melodic resolution",
        "instrumentalCaption": "recognizable melodic theme, recurring motif, melodic lift and resolution",
        "music3Fields": {"primary": "A distinctive short instrumental pickup previews the sung hook and establishes the groove.",
                         "style": "Each written chorus repeats a short melodic-and-rhythmic motif answered by a gentle falling phrase, and the final line resolves warmly."},
        "music3InstrumentalFields": {"primary": "A distinctive short opening theme establishes the groove and returns in the fuller reprise, answered by a gentle falling phrase before the final phrase resolves warmly."},
    },
    {
        "id": "expressive-performance", "label": "보컬 감정 살리기",
        "description": "가사의 감정이 프레이징·음의 길이·강약으로 드러나도록 보컬 표현을 안내해요. 연주곡에서는 적용하지 않아요.",
        "caption": "emotionally connected lead vocal, shaped phrasing, intentional dynamics and sustained line endings",
        "music3Fields": {"style": "The performance is emotionally connected: intimate verse phrasing opens into a warmer, more sustained chorus, with intentional dynamics and natural line endings."},
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
