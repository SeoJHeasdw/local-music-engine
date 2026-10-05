"""MiniMax Music 3 contract and frozen musical instruction translation."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .lyrics import is_instrumental_lyrics

ENGINE = "minimax-music3"
MODEL = "mlx-community/MiniMax-Music3-bf16"
BASE_URL = "http://127.0.0.1:18002"
MAX_DURATION_SECONDS = 300
CAPABILITIES = {"text2music": True, "cover": False, "repaint": False, "referenceAudio": False}
_STANDARD_TAG = re.compile(r"^\s*\[(intro|verse(?:\s+\d+)?|chorus(?:\s+\d+)?|pre[- ]chorus|post[- ]chorus|bridge|outro|instrumental|hook|break|refrain|interlude)(?:\s*[-:]\s*([^\]]+))?\]\s*$", re.I)
_LEADING_TAG = re.compile(r"^\s*(\[[^\]\r\n]*\])\s*")
PROMPT_VERSION = 4
# MiniMax's published caption library (skills/music-caption-rewriter/templates in
# MiniMax-AI/MiniMax-Music3) uses exactly these bare headings and field labels in
# all 1,000 examples. Bracketed headings, "Tempo:" lines, imperative rules and
# exclusion lists are outside that distribution, so requests render into it.
CAPTION_SCHEMA: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Global Metadata", (("basic", "Basic Attributes"), ("emotion", "Global Emotional Progression"),
                         ("scenario", "Application Scenarios & Imagery"), ("sonics", "Sonics & Production Profile"))),
    ("Vocal Details", (("timbre", "Vocal Gender & Timbre"), ("style", "Vocal Style"),
                       ("harmony", "Harmony/Backing Vocals"), ("fx", "Vocal FX"))),
    ("Arrangement", (("lifecycle", "Instrument Lifecycle Description (Primary/Secondary Layering)"),
                     ("primary", "Primary"), ("secondary", "Secondary"),
                     ("groove", "Groove & Foundation Progression"),
                     ("textures", "Embellishments, Textures & Spatial FX"))),
)
CAPTION_FIELDS = tuple(key for _, fields in CAPTION_SCHEMA for key, _ in fields if key != "lifecycle")
# Pre-v4 snapshots froze one sentence per rule; place it where that rule acts.
DEFAULT_RULE_FIELD = {
    "steady-groove": "groove", "repeated-harmony": "primary", "focused-arrangement": "secondary",
    "clear-vocal": "style", "clean-production": "sonics", "simple-structure": "emotion",
    "section-development": "groove", "phrase-breathing": "style", "melodic-hook": "style",
    "expressive-performance": "style",
}
_LANGUAGES = {"ko": "Korean", "en": "English", "ja": "Japanese", "zh": "Mandarin Chinese"}
# The library spells every key with these twelve names (Bb, Eb, Ab, F#, C#).
_KEY_SPELLING = {"c": "C", "c#": "C#", "db": "C#", "d": "D", "d#": "Eb", "eb": "Eb", "e": "E", "fb": "E",
                 "e#": "F", "f": "F", "f#": "F#", "gb": "F#", "g": "G", "g#": "Ab", "ab": "Ab", "a": "A",
                 "a#": "Bb", "bb": "Bb", "b": "B", "cb": "B", "b#": "C"}
_KEY = re.compile(r"^\s*([A-Ga-g])\s*([#♯b♭]?)\s*(major|minor|maj|min|m)?\s*$", re.I)
_FEMALE = re.compile(r"\b(?:female|woman|women|girl|feminine)\b|여성|여자|여가수", re.I)
_MALE = re.compile(r"\b(?:male|man|men|boy|masculine)\b|남성|남자|남가수", re.I)
_HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?\[?\s*(global metadata|vocal details|arrangement)\s*\]?\s*:?\s*$", re.I)
_LABELLED = re.compile(r"^\s*(?:[-*+•]\s+)?(?:\*\*)?([^:*\n]{3,80}?)(?:\*\*)?\s*:\s*(.*)$")
_FIELD_BY_LABEL = {label.casefold(): key for _, fields in CAPTION_SCHEMA for key, label in fields}
_UNLABELLED_FIELD = {"global metadata": "emotion", "vocal details": "style", "arrangement": "groove"}


def _prepare_lyrics(lyrics: str) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Upstream consumes an entire tag-led line: keep its sung suffix separately."""
    lines, instructions, changes = [], [], []
    for number, line in enumerate(lyrics.split("\n"), 1):
        tags, remainder = [], line
        while match := _LEADING_TAG.match(remainder):
            tags.append(match[1])
            remainder = remainder[match.end():]
        if tags and (remainder or len(tags) > 1):
            units = [*tags, *([remainder] if remainder else [])]
            changes.append({"kind": "inline_tags_separated", "line": number, "tagCount": len(tags)})
        else:
            units = [line]
        for unit in units:
            match = _STANDARD_TAG.fullmatch(unit)
            if match and match[2]:
                lines.append(f"[{match[1]}]")
                instructions.append(f"{match[1]}: {match[2].strip()}.")
                changes.append({"kind": "section_direction_moved_to_caption", "line": number, "section": match[1]})
            else:
                lines.append(unit)
    return "\n".join(lines), instructions, changes


def plain_lyrics(lyrics: str) -> tuple[str, list[str]]:
    """Preserve sung words while separating tag lines and performance directions."""
    prepared, instructions, _ = _prepare_lyrics(lyrics)
    return prepared, instructions


def _arrangement_schedule(plan: dict[str, Any], *, instrumental: bool, lyrics: str) -> dict[str, Any]:
    """Keep numerical estimates inspectable, but condition on musical section roles.

    The layout is an advisory preview and may contain an unwritten pre-chorus or
    intro. Feeding that timetable to the planner can compete with the actual
    lyrics. Only the supplied lyric sections define the generated vocal order.
    """
    source = plan["arrangement"]
    counts: dict[str, int] = {}
    sections = []
    for section in source:
        label = section["label"]
        occurrence = counts.get(label, 0) + 1
        counts[label] = occurrence
        sections.append({**deepcopy(section), "occurrence": occurrence})
    lyric_sections = [match[1] for line in lyrics.splitlines()
                      if (match := _STANDARD_TAG.fullmatch(line))]
    if instrumental:
        caption = "A restrained main theme opens into a fuller reprise with melodic lift, then a brief resolved ending."
    else:
        caption = "The song follows its written sections in order: intimate verses open into fuller choruses with melodic lift, connected through the same groove, before a brief musical resolution."
    return {"version": 3, "source": "songPlan.arrangement+lyrics", "advisory": True,
            "captionMode": "musical_section_roles", "sections": sections,
            "lyricSections": lyric_sections, "caption": caption}


def _sentence(text: str) -> str:
    text = " ".join(str(text).split())
    if not text:
        return ""
    if text[0].isascii() and text[0].islower():
        text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def _append(fields: dict[str, str], key: str, text: str) -> None:
    text = _sentence(text)
    if text and text.casefold() not in fields.get(key, "").casefold():
        fields[key] = f"{fields[key]} {text}" if fields.get(key) else text


def key_attributes(key_scale: Any) -> str | None:
    """``A minor`` -> ``key is A, and scale is minor``; unparsed values stay readable."""
    if key_scale in (None, ""):
        return None
    match = _KEY.match(str(key_scale))
    if not match:
        return f"key is {' '.join(str(key_scale).split())}"
    accidental = {"♯": "#", "♭": "b", "B": "b"}.get(match[2], match[2])
    key = _KEY_SPELLING.get((match[1] + accidental).casefold(), match[1].upper() + accidental)
    mode = (match[3] or "").casefold()
    scale = "minor" if mode in {"minor", "min", "m"} else "major" if mode in {"major", "maj"} else None
    return f"key is {key}, and scale is {scale}" if scale else f"key is {key}"


def basic_attributes(bpm: Any, key_scale: Any) -> str:
    parts = []
    if bpm not in (None, ""):
        value = float(bpm)
        parts.append(f"bpm is {int(value) if value.is_integer() else value:g}")
    if key := key_attributes(key_scale):
        parts.append(key)
    return " ".join(f"{part}." for part in parts)


def parse_structured_caption(text: str) -> dict[str, str] | None:
    """Read a caption already written in the three-heading schema, else ``None``."""
    headings: set[str] = set()
    fields: dict[str, str] = {}
    heading = current = None
    for line in text.splitlines():
        if match := _HEADING.match(line):
            heading = match[1].casefold()
            headings.add(heading)
            current = None
            continue
        if not line.strip() or heading is None:
            continue
        labelled = _LABELLED.match(line)
        key = _FIELD_BY_LABEL.get(labelled[1].strip().casefold()) if labelled else None
        if key == "lifecycle":
            current = None
            continue
        if key:
            current = key
            line = labelled[2]
        target = current or _UNLABELLED_FIELD[heading]
        if line.strip():
            fields[target] = f"{fields[target]} {line.strip()}" if fields.get(target) else line.strip()
    return fields if len(headings) == 3 and fields else None


def _genre_label(basic: str) -> str:
    """Keep a structured caption's genre words; metas come from the frozen request."""
    text = re.sub(r"<\|[^|]*\|>\.?", "", basic)
    text = re.sub(r"\bbpm is [^.]*\.|\bkey is [^.]*\.|\bscale is [^.]*\.", "", text, flags=re.I)
    text = re.sub(r"\b(?:tempo|key|meter|time signature)\s*:\s*[^.]*\.", "", text, flags=re.I)
    return " ".join(text.split()).strip(" .")


def _vocal_identity(style: str) -> str | None:
    found = sorted((match.start(), gender) for gender, pattern in (("Female", _FEMALE), ("Male", _MALE))
                   if (match := pattern.search(style)))
    if len(found) == 2:
        return f"Singer A ({found[0][1]}) and Singer B ({found[1][1]})"
    return f"Singer A ({found[0][1]})" if found else None


def _preset_fields(preset: dict[str, Any] | None, *, instrumental: bool) -> tuple[dict[str, str], bool]:
    """Read only the frozen snapshot; pre-v4 snapshots keep their own wording."""
    if not preset:
        return {}, False
    schema = preset.get("music3InstrumentalSchema" if instrumental else "music3Schema") or preset.get("music3Schema")
    if schema:
        return {key: value for key, value in schema.items() if isinstance(value, str) and value.strip()}, True
    if preset.get("music3Caption"):
        legacy = preset.get("music3InstrumentalCaption", preset["music3Caption"]) if instrumental else preset["music3Caption"]
        return {"emotion": legacy}, False
    return {"genre": preset["caption"]}, False


def _rule_fields(rule_id: str, rule: dict[str, Any] | None, fallback: str, *,
                 duration: float, instrumental: bool) -> tuple[dict[str, str], bool]:
    """Read only frozen rule text, never today's catalog, for source regeneration."""
    short = duration <= 45
    if rule and "music3Fields" in rule:
        names = (["music3ShortInstrumentalFields"] if short and instrumental else []) + \
                (["music3InstrumentalFields"] if instrumental else []) + \
                (["music3ShortFields"] if short and not instrumental else []) + ["music3Fields"]
        return deepcopy(next(rule[name] for name in names if name in rule)), True
    caption = fallback
    if rule and "music3Caption" in rule:
        caption = rule["music3Caption"]
        if instrumental:
            caption = rule.get("music3InstrumentalCaption", caption)
        if short:
            caption = rule.get("music3ShortInstrumentalCaption" if instrumental else "music3ShortCaption", caption)
    return {DEFAULT_RULE_FIELD.get(rule_id, "groove"): caption}, False


def render_caption(fields: dict[str, str]) -> str:
    lines = []
    for heading, entries in CAPTION_SCHEMA:
        body = []
        for key, label in entries:
            if key == "lifecycle":
                if fields.get("primary") or fields.get("secondary"):
                    body.append(f"{label}:")
            elif fields.get(key):
                body.append(f"{label}: {fields[key]}")
        if body:
            lines += [heading, *body]
    return "\n".join(lines)


def translate_payload(payload: dict[str, Any], *, base_style: str | None = None) -> dict[str, Any]:
    """Render Music 3's trained structured caption without feeding ACE-specific options to it."""
    result = deepcopy(payload)
    # Rebuild only from currently selected rules; a previous preparation must not
    # leave a timing schedule behind after section development is deselected.
    result.pop("music3ArrangementSchedule", None)
    result.pop("music3ProductionGuidance", None)
    snapshot = result.get("productionRules") or {}
    style = base_style if base_style is not None else result.get("sourceStylePrompt", snapshot.get("baseStylePrompt", result["prompt"]))
    lyrics, section_details, lyric_changes = _prepare_lyrics(result["lyrics"])
    previous = result.get("music3LyricPreparation") or {}
    section_details = list(dict.fromkeys([*previous.get("sectionInstructions", []), *section_details]))
    instrumental = is_instrumental_lyrics(lyrics)
    duration = float(result["audio_duration"])
    captions = list(snapshot.get("appliedCaptions", []))
    rule_ids = snapshot.get("appliedRuleIds", [])
    preset = snapshot.get("preset")
    native_preset = None
    fields: dict[str, str] = {}
    if preset and captions and captions[0] == preset["caption"]:
        captions.pop(0)
        fields, native = _preset_fields(preset, instrumental=instrumental)
        native_preset = {"id": preset["id"], "fields": deepcopy(fields), "native": native}
    if len(captions) != len(rule_ids):
        raise ValueError("production rule captions do not match their frozen rule IDs")
    genre = fields.pop("genre", "")
    timbre = fields.pop("timbre", "")
    lead = fields.pop("lead", "")
    structured = parse_structured_caption(style)
    if structured:
        # An explicitly structured caption (e.g. from an assistant) wins per field.
        genre = _genre_label(structured.pop("basic", "")) or genre
        if structured.get("timbre"):
            timbre = ""
        fields.update(structured)
        user_style = ""
    else:
        user_style = style.strip()
    basic = [basic_attributes(result.get("bpm"), result.get("key_scale")), _sentence(genre)]
    if user_style and user_style.casefold() not in genre.casefold():
        basic.append(_sentence(user_style))
    fields["basic"] = " ".join(part for part in basic if part)
    if not fields.get("timbre"):
        if instrumental:
            fields["timbre"] = " ".join(part for part in ("Instrumental; there is no vocal.", _sentence(lead)) if part)
        else:
            identity = _vocal_identity(user_style) if user_style else None
            texture = timbre or "a clear, expressive timbre"
            if identity and " and Singer B" in identity:
                fields["timbre"] = f"{identity}. The vocalists share {texture}."
            elif identity:
                fields["timbre"] = f"{identity}. The vocalist has {texture}."
            else:
                fields["timbre"] = f"A single lead vocalist delivers the performance with {texture}."
    if not instrumental and (language := _LANGUAGES.get(str(result.get("vocal_language", "ko")))):
        _append(fields, "style", f"The lyrics are sung in {language}")
    if str(result.get("time_signature") or "4") not in {"4", "4/4"}:
        meter = {"2": "2/4", "3": "3/4", "6": "6/8"}.get(str(result["time_signature"]), str(result["time_signature"]))
        _append(fields, "groove", f"The song moves in {meter} time")
    frozen_native_rules = []
    rules_by_id = {rule["id"]: rule for rule in snapshot.get("rules", [])}
    for rule, caption in zip(rule_ids, captions):
        rule_fields, native = _rule_fields(rule, rules_by_id.get(rule), caption, duration=duration, instrumental=instrumental)
        frozen_native_rules.append({"ruleId": rule, "fields": rule_fields, "native": native})
        for key, text in rule_fields.items():
            _append(fields, key if key in CAPTION_FIELDS else "groove", text)
    plan = result.get("songPlan") or {}
    options = plan.get("options") or {}
    selected = set(snapshot.get("selection", {}).get("ruleIds", []))
    if ("section-development" in selected and "section-development" in rule_ids
            and options.get("development") and plan.get("arrangement")):
        schedule = _arrangement_schedule(plan, instrumental=instrumental, lyrics=lyrics)
        result["music3ArrangementSchedule"] = schedule
        _append(fields, "emotion", schedule["caption"])
    if ("phrase-breathing" in selected and "phrase-breathing" in rule_ids
            and options.get("breathing") and not instrumental):
        # Exact syllables/breath beats are estimates shown in the plan. The model
        # receives the independent musical phrase direction, rather than numbers
        # that compete with its own timing of the supplied words.
        if not any(rule["ruleId"] == "phrase-breathing" and rule["native"] for rule in frozen_native_rules):
            offset = 2 if options.get("development") else 0
            for guidance in plan.get("guidance", [])[offset:]:
                _append(fields, "style", guidance)
    for detail in section_details:
        _append(fields, "groove", detail)
    if result.get("project_structure"):
        _append(fields, "emotion", str(result["project_structure"]))
    if not any(fields.get(key) for key in ("primary", "secondary", "groove", "textures")):
        fields["groove"] = "The arrangement develops naturally across the written sections with coherent transitions."
    result.update(engine=ENGINE, sourceStylePrompt=style, lyrics=lyrics,
                  prompt=render_caption(fields), inference_steps=30, music3PromptVersion=PROMPT_VERSION,
                  music3ProductionGuidance={"version": 2, "source": "frozen_production_rules",
                                           "captionSchema": "minimax-music3-structured-caption",
                                           "structuredStyle": structured is not None,
                                           "preset": native_preset,
                                           "rules": frozen_native_rules,
                                           "sectionTimingInCaption": False})
    changes = deepcopy(previous.get("changes", []))
    for change in lyric_changes:
        if change not in changes:
            changes.append(change)
    result["music3LyricPreparation"] = {"version": 1,
        "lyricsOriginal": result.get("sourceLyricsOriginal", previous.get("lyricsOriginal", payload["lyrics"])),
        "lyricsPrepared": lyrics, "changes": changes, "sectionInstructions": section_details}
    for key in ("thinking", "lm_model_path", "lm_backend", "lm_temperature", "use_cot_caption", "use_cot_language", "constrained_decoding", "guidance_scale", "use_random_seed"):
        result.pop(key, None)
    return result
