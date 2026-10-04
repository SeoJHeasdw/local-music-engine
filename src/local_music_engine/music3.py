"""MiniMax Music 3 contract and frozen musical instruction translation."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .lyrics import is_instrumental_lyrics

ENGINE = "minimax-music3"
MODEL = "mlx-community/MiniMax-Music3-mxfp8"
BASE_URL = "http://127.0.0.1:18002"
MAX_DURATION_SECONDS = 300
CAPABILITIES = {"text2music": True, "cover": False, "repaint": False, "referenceAudio": False}
_STANDARD_TAG = re.compile(r"^\s*\[(intro|verse(?:\s+\d+)?|chorus(?:\s+\d+)?|pre[- ]chorus|post[- ]chorus|bridge|outro|instrumental|hook|break|refrain|interlude)(?:\s*[-:]\s*([^\]]+))?\]\s*$", re.I)
_LEADING_TAG = re.compile(r"^\s*(\[[^\]\r\n]*\])\s*")


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
        caption = "A restrained main theme opens into a fuller reprise with melodic lift, then a brief resolved ending; no vocals."
    else:
        caption = "Follow the supplied lyric sections in order. Intimate verses open into fuller choruses with melodic lift; connect them through the same groove and finish with a brief musical resolution."
    return {"version": 2, "source": "songPlan.arrangement+lyrics", "advisory": True,
            "captionMode": "musical_section_roles", "sections": sections,
            "lyricSections": lyric_sections, "caption": caption}


def _native_rule_caption(rule: dict[str, Any] | None, fallback: str, *,
                         duration: float, instrumental: bool) -> tuple[str, bool]:
    """Read only frozen rule text, never today's catalog, for source regeneration."""
    if not rule or "music3Caption" not in rule:
        return fallback, False
    caption = rule["music3Caption"]
    if instrumental:
        caption = rule.get("music3InstrumentalCaption", caption)
    if duration <= 45:
        caption = rule.get("music3ShortInstrumentalCaption" if instrumental else "music3ShortCaption", caption)
    return caption, True


def translate_payload(payload: dict[str, Any], *, base_style: str | None = None) -> dict[str, Any]:
    """Use Music 3's structured caption without feeding ACE-specific options to it."""
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
    metadata = [style.strip(), f"Duration: {result['audio_duration']:g} seconds."]
    captions = list(snapshot.get("appliedCaptions", []))
    rule_ids = snapshot.get("appliedRuleIds", [])
    preset = snapshot.get("preset")
    native_preset = None
    if preset and captions and captions[0] == preset["caption"]:
        preset_caption = captions.pop(0)
        preset_caption, native = _native_rule_caption(preset, preset_caption,
            duration=float(result["audio_duration"]), instrumental=instrumental)
        native_preset = {"id": preset["id"], "caption": preset_caption, "native": native}
        if preset_caption.casefold() not in style.casefold():
            metadata.append(preset_caption)
    if len(captions) != len(rule_ids):
        raise ValueError("production rule captions do not match their frozen rule IDs")
    for key, label in (("bpm", "Tempo"), ("key_scale", "Key"), ("time_signature", "Meter")):
        if result.get(key) not in (None, ""):
            suffix = " BPM" if key == "bpm" else ""
            value = {"2": "2/4", "3": "3/4", "4": "4/4", "6": "6/8"}.get(str(result[key]), result[key]) if key == "time_signature" else result[key]
            metadata.append(f"{label}: {value}{suffix}.")
    vocal = ["Instrumental; no vocals." if instrumental else f"Vocal language: {result.get('vocal_language', 'ko')}; sing the supplied lyrics in order."]
    arrangement, frozen_native_rules = [], []
    rules_by_id = {rule["id"]: rule for rule in snapshot.get("rules", [])}
    for rule, caption in zip(rule_ids, captions):
        native_caption, native = _native_rule_caption(rules_by_id.get(rule), caption,
            duration=float(result["audio_duration"]), instrumental=instrumental)
        frozen_native_rules.append({"ruleId": rule, "caption": native_caption, "native": native})
        (vocal if rule in {"clear-vocal", "phrase-breathing", "expressive-performance"} else arrangement).append(native_caption)
    plan = result.get("songPlan") or {}
    options = plan.get("options") or {}
    selected = set(snapshot.get("selection", {}).get("ruleIds", []))
    if ("section-development" in selected and "section-development" in rule_ids
            and options.get("development") and plan.get("arrangement")):
        schedule = _arrangement_schedule(plan, instrumental=instrumental, lyrics=lyrics)
        result["music3ArrangementSchedule"] = schedule
        arrangement.append(schedule["caption"])
    if ("phrase-breathing" in selected and "phrase-breathing" in rule_ids
            and options.get("breathing") and not instrumental):
        # Exact syllables/breath beats are estimates shown in the plan. The model
        # receives the independent musical phrase direction, rather than numbers
        # that compete with its own timing of the supplied words.
        if not any(rule["ruleId"] == "phrase-breathing" and rule["native"] for rule in frozen_native_rules):
            offset = 2 if options.get("development") else 0
            vocal.extend(plan.get("guidance", [])[offset:])
    arrangement.extend(section_details)
    if result.get("project_structure"):
        arrangement.append(str(result["project_structure"]))
    def joined(values: list[str]) -> str:
        return " ".join(dict.fromkeys(value.strip() for value in values if value.strip()))
    result.update(engine=ENGINE, sourceStylePrompt=style, lyrics=lyrics,
                  prompt=f"[Global Metadata]\n{joined(metadata)}\n\n[Vocal Details]\n{joined(vocal)}\n\n[Arrangement]\n{joined(arrangement) or 'Follow the supplied song sections with coherent transitions.'}",
                  inference_steps=30, music3PromptVersion=3,
                  music3ProductionGuidance={"version": 1, "source": "frozen_production_rules",
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
