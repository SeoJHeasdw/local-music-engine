"""Lightweight, deterministic section and lyric preparation; timings are advisory.

Hangul syllable blocks are counted exactly. English syllables use vowel groups and
simple silent-ending rules, so their counts and all performance timings are estimates.
The planner retains every sung word in order and never creates missing sections.
"""

from __future__ import annotations

import math
import re
from typing import Any

from . import production_rules

_HANGUL = re.compile(r"[가-힣]")
_ENGLISH = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_TAG = re.compile(r"^\s*\[([^\]\r\n]+)\]\s*$")
_STANDARD_TAG = re.compile(r"^(intro|verse|pre[- ]?chorus|chorus|hook|bridge|outro)(?:\s+\d+)?$", re.IGNORECASE)
_BREAK = re.compile(r"\s+|(?<=[,.;:!?，。！？；：…])(?=\S)")
_INPUT_FIELDS = {"lyrics", "durationSeconds", "bpm", "timeSignature", "presetId", "instrumental", "development", "breathing"}
_METHOD = "hangul-exact+english-heuristic"


def _finite_number(value: Any, minimum: float, maximum: float, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be a finite number between {minimum:g} and {maximum:g}")
    return float(value)


def validate_song_plan_input(value: Any) -> dict[str, Any]:
    """Validate the renderer/CLI request before preparing lyrics or touching files."""

    if not isinstance(value, dict) or set(value) != _INPUT_FIELDS:
        raise ValueError("song plan must contain lyrics, durationSeconds, bpm, timeSignature, presetId, instrumental, development and breathing only")
    if not isinstance(value["lyrics"], str) or len(value["lyrics"]) > 4096:
        raise ValueError("song plan lyrics must be a string of at most 4096 characters")
    _finite_number(value["durationSeconds"], 10, 600, "duration")
    if value["bpm"] is not None:
        _finite_number(value["bpm"], 30, 300, "bpm")
    production_rules.normalize_time_signature(value["timeSignature"])
    if value["presetId"] is not None and (not isinstance(value["presetId"], str)
            or value["presetId"] not in {preset["id"] for preset in production_rules.PRESETS}):
        raise ValueError("unknown song plan preset")
    for key in ("instrumental", "development", "breathing"):
        if type(value[key]) is not bool:
            raise ValueError(f"song plan {key} must be a boolean")
    return dict(value)


def _english_syllables(word: str) -> int:
    text = word.lower().replace("'", "").replace("’", "")
    groups = len(re.findall(r"[aeiouy]+", text))
    if text.endswith("e") and not text.endswith(("le", "ye")) and groups > 1:
        groups -= 1
    if text.endswith("ed") and not text.endswith(("ted", "ded")) and groups > 1:
        groups -= 1
    return max(1, groups)


def count_syllables(text: str) -> int:
    """Count Korean syllable blocks and estimate English; other scripts stay intact."""

    return len(_HANGUL.findall(text)) + sum(_english_syllables(word) for word in _ENGLISH.findall(text))


def _phrase_settings(preset_id: str | None, bpm: float, quarter_beats: float, duration: float) -> dict[str, Any]:
    rate = 3.0 if preset_id == "emotional-hiphop" else 2.4 if preset_id == "upbeat-pop" else 2.25
    breath = 0.5 if preset_id == "emotional-hiphop" else 0.75
    phrase_bars = 1.0 if duration <= 45 else 1.5
    target = round(rate * max(1, quarter_beats - breath) * phrase_bars * (bpm / 96) ** 0.15)
    return {"syllablesPerBeat": rate, "breathAfterBeats": breath, "targetSyllables": max(4, min(20, target))}


def _safe_boundaries(text: str) -> list[tuple[int, int]]:
    result = []
    for match in _BREAK.finditer(text):
        start, end = match.span()
        if not 0 < start or end >= len(text):
            continue
        # A decimal point is part of its token, rather than a place to breathe.
        if start == end and start >= 2 and text[start - 1] == "." and text[start - 2].isdigit() and text[start].isdigit():
            continue
        result.append((start, end))
    return result


def _split_line(text: str, target: int) -> list[str]:
    """Replace only existing internal whitespace, or insert a break after punctuation."""

    if count_syllables(text) <= target:
        return [text]
    boundaries = _safe_boundaries(text)
    if not boundaries:
        return [text]
    candidates = []
    for start, end in boundaries:
        syllables = count_syllables(text[:start])
        if syllables > target:
            break
        if syllables:
            candidates.append((start, end))
    if candidates:
        punctuation = [(start, end) for start, end in candidates
                       if text[start - 1] in ",.;:!?，。！？；：…" and count_syllables(text[:start]) >= target * 0.6]
        start, end = (punctuation or candidates)[-1]
    else:
        # Keep an overlong indivisible word intact, then make room after it.
        start, end = boundaries[0]
    return [text[:start], *_split_line(text[end:], target)]


def _instruments(preset_id: str | None) -> list[str]:
    return ["drums", "bass", "piano" if preset_id == "emotional-hiphop" else "keys" if preset_id == "upbeat-pop" else "main chord instrument"]


def _section_details(label: str, preset_id: str | None) -> tuple[float, str]:
    kind = re.sub(r"[\s-]+", "", label.lower())
    if kind.startswith(("chorus", "hook")) or kind == "themereprise":
        return (0.76 if preset_id == "emotional-hiphop" else 0.9, "fuller groove, clear main hook")
    if kind.startswith(("prechorus", "bridge")) or kind == "build":
        return (0.62, "gradual lift, steady groove")
    if kind.startswith("outro"):
        return (0.25, "gentle resolved ending")
    if kind.startswith("intro"):
        return (0.25, "light main instrument, brief entrance")
    return (0.44 if preset_id == "emotional-hiphop" else 0.5, "restrained groove, space for the main phrase")


def _arrangement(duration: float, seconds_per_bar: float, preset_id: str | None, instrumental: bool) -> list[dict[str, Any]]:
    total_bars = duration / seconds_per_bar
    if duration <= 45 or total_bars < 12:
        labels = ["Theme", "Theme reprise", "Outro"] if instrumental else ["Verse", "Chorus", "Outro"]
        weights = [0.45, 0.45, 0.1]
    else:
        labels = ["Intro", "Theme", "Build", "Theme reprise", "Theme", "Theme reprise", "Outro"] if instrumental else ["Intro", "Verse", "Pre-Chorus", "Chorus", "Verse", "Chorus", "Outro"]
        weights = [0.08, 0.18, 0.1, 0.2, 0.16, 0.2, 0.08]
    whole_bars = math.floor(total_bars)
    if whole_bars >= len(labels):
        counts = [max(1, math.floor(whole_bars * weight)) for weight in weights]
        while sum(counts) > whole_bars:
            choices = [index for index, count in enumerate(counts) if count > 1]
            index = max(choices, key=lambda item: counts[item] - whole_bars * weights[item])
            counts[index] -= 1
        while sum(counts) < whole_bars:
            index = max(range(len(counts)), key=lambda item: whole_bars * weights[item] - counts[item])
            counts[index] += 1
        bars = [float(count) for count in counts]
        bars[-1] += total_bars - whole_bars
    else:
        bars = [total_bars * weight for weight in weights]
    result = []
    start = 0.0
    for index, (label, count) in enumerate(zip(labels, bars, strict=True)):
        end = duration if index == len(labels) - 1 else min(duration, start + count * seconds_per_bar)
        energy, guidance = _section_details(label, preset_id)
        result.append({"label": label, "bars": round(count, 4), "startSeconds": round(start, 6),
                       "endSeconds": round(end, 6), "energy": energy, "instruments": _instruments(preset_id), "guidance": guidance})
        start = end
    return result


def _add_section_guidance(text: str, tag: str, preset_id: str | None) -> str:
    energy, _ = _section_details(tag, preset_id)
    level = "fuller" if energy >= 0.7 else "building" if energy >= 0.6 else "gentle" if energy < 0.3 else "restrained"
    return text.replace(f"[{tag}]", f"[{tag} - {level}]", 1)


def prepare_song_plan(
    lyrics: str, *, duration_seconds: float, bpm: int | float | None,
    time_signature: str | int | None, preset_id: str | None, instrumental: bool,
    development: bool, breathing: bool,
) -> dict[str, Any]:
    """Prepare an inspectable plan without changing the input or touching audio/files."""

    validate_song_plan_input({"lyrics": lyrics, "durationSeconds": duration_seconds, "bpm": bpm,
                              "timeSignature": time_signature, "presetId": preset_id, "instrumental": instrumental,
                              "development": development, "breathing": breathing})
    duration = float(duration_seconds)
    preset = next((item for item in production_rules.PRESETS if item["id"] == preset_id), None)
    tempo = float(bpm if bpm is not None else preset["bpm"] if preset else 96)
    meter = production_rules.normalize_time_signature(time_signature) or (preset["timeSignature"] if preset else "4")
    quarter_beats = 3.0 if meter == "6" else float(meter)
    seconds_per_bar = 60 / tempo * quarter_beats
    settings = _phrase_settings(preset_id, tempo, quarter_beats, duration)
    warnings: list[str] = []
    changes: list[dict[str, Any]] = []
    phrases: list[dict[str, Any]] = []
    output: list[str] = []
    section: str | None = None
    for line_index, raw in enumerate(lyrics.splitlines(keepends=True), start=1):
        eol_match = re.search(r"(?:\r\n|\r|\n)$", raw)
        eol = eol_match.group(0) if eol_match else ""
        text = raw[:-len(eol)] if eol else raw
        tag_match = _TAG.fullmatch(text)
        if tag_match:
            tag = tag_match.group(1)
            section = tag
            prepared = text
            if development and _STANDARD_TAG.fullmatch(tag):
                prepared = _add_section_guidance(text, tag, preset_id)
                if prepared != text:
                    changes.append({"kind": "section-guidance", "lineIndex": line_index, "before": text, "after": prepared})
            output.append(prepared + eol)
            continue
        if not text.strip() or instrumental:
            output.append(raw)
            continue
        parts = _split_line(text, settings["targetSyllables"]) if breathing else [text]
        prepared = (eol or "\n").join(parts)
        if prepared != text:
            changes.append({"kind": "line-break", "lineIndex": line_index, "before": text, "after": prepared})
        output.append(prepared + eol)
        for phrase_index, part in enumerate(parts, start=1):
            syllables = count_syllables(part)
            estimated_bars = max(0.5, math.ceil((syllables / settings["syllablesPerBeat"] + settings["breathAfterBeats"]) / quarter_beats * 2) / 2)
            phrases.append({"section": section, "lineIndex": line_index, "phraseIndex": phrase_index,
                            "text": part.strip(), "syllables": syllables, "syllableMethod": _METHOD,
                            "estimatedBars": estimated_bars, "breathAfterBeats": settings["breathAfterBeats"],
                            "targetSyllables": settings["targetSyllables"]})
            if breathing and syllables > settings["targetSyllables"]:
                warnings.append(f"{line_index}번째 줄에 안전하게 더 나눌 수 없는 긴 구절이 있어요. 단어는 유지했으니 직접 호흡을 확인해 주세요.")
    arrangement = _arrangement(duration, seconds_per_bar, preset_id, instrumental) if development else []
    if development or breathing:
        warnings.insert(0, "구간 시간과 가사 호흡은 제작 안내용 추정이에요. 엔진이 박자·구간 길이를 정확히 지키는지는 실제 음원으로 확인해 주세요.")
    if development and any(_TAG.fullmatch(line) for line in lyrics.splitlines()):
        warnings.append("구성표는 권장 배분이며, 입력한 가사의 섹션 순서와 문구는 유지해요.")
    if any(_ENGLISH.search(phrase["text"]) for phrase in phrases):
        warnings.append("한글은 음절 글자 수로 세고, 영어 음절 수는 발음 사전 없이 추정해요.")
    if meter == "6" and (development or breathing):
        warnings.append("6/8박자의 마디 길이는 BPM을 4분음표 기준으로 가정해 계산했어요.")
    if phrases and breathing:
        estimated_seconds = sum(phrase["estimatedBars"] for phrase in phrases) * seconds_per_bar
        usable_seconds = duration - min(3, duration * 0.1) if development else duration
        if estimated_seconds > usable_seconds:
            warnings.append("가사가 요청한 길이에 비해 촘촘해요. 모든 단어는 유지했으니 길이를 늘리거나 가사를 직접 줄여 주세요.")
    if development and duration > 45 and duration / seconds_per_bar < 12:
        warnings.append("현재 길이와 템포에서는 마디가 적어 한 구절과 후렴 중심으로 전개를 간단하게 제안했어요.")
    guidance: list[str] = []
    if development:
        guidance.append("Short verse to fuller hook, then brief resolved ending." if duration <= 45 else "Brief intro, restrained verses, gradual build, fuller recurring chorus, gentle ending.")
        if instrumental:
            guidance[-1] = "Restrained main theme, fuller reprise, brief gentle ending."
        guidance.append("Keep existing lyric sections and word order.")
    if breathing and not instrumental:
        guidance.append(f"At {tempo:g} BPM, keep lyric lines near {settings['targetSyllables']} syllables with {settings['breathAfterBeats']:g} beats of breathing space; preserve all words.")
    return {"version": 1, "lyricsOriginal": lyrics, "lyricsPrepared": "".join(output), "changes": changes,
            "arrangement": arrangement, "phrases": phrases, "warnings": list(dict.fromkeys(warnings)),
            "timing": {"bpm": tempo, "timeSignature": meter, "quarterBeatsPerBar": quarter_beats,
                       "secondsPerBar": round(seconds_per_bar, 6), "estimatedTotalBars": round(duration / seconds_per_bar, 4), "bpmBeatUnit": "quarter-note"},
            "options": {"presetId": preset_id, "instrumental": instrumental, "development": development, "breathing": breathing},
            "guidance": guidance}
