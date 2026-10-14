"""Source-preserving lyric planning and conservative STT comparison.

These are diagnostics, not proof of singing quality or a human listening verdict.
Density and retry thresholds are application heuristics, not universal limits for
music. Recognition failures produce unknown results, never automatic success.
"""

from __future__ import annotations

import math
import re
import unicodedata
from array import array
from collections import Counter
from typing import Any, Sequence

_TAG = re.compile(r"\[([^\]\n]+)\]")
_UNITS = re.compile(r"[가-힣]|[a-z]+(?:['’][a-z]+)*", re.IGNORECASE)
_HANGUL = re.compile(r"[가-힣]")
_LANGUAGE_TAGS = {"ko", "en", "korean", "english"}
_CHORUS_TAG = re.compile(r"\b(?:chorus|hook|refrain)\b|후렴", re.IGNORECASE)
_REPEAT = re.compile(r"(?:\bx\s*(\d+)\b|\b(\d+)\s*x\b)", re.IGNORECASE)
_MAX_ALIGNMENT_CELLS = 2_000_000
_MAX_TEXT_CHARS = 24_000
_HALLUCINATION_PHRASES = (
    "thankyouforwatching", "thanksforwatching", "subtitlesby",
    "시청해주셔서감사합니다", "구독과좋아요",
)


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("’", "'")


def _units(text: str) -> list[str]:
    text = _TAG.sub(" ", _normalized(text))
    return [("ko:" if _HANGUL.fullmatch(item) else "en:") + item.replace("'", "")
            for item in _UNITS.findall(text)]


def _unsupported_content(text: str) -> bool:
    return any((unicodedata.category(char).startswith("L") and not _HANGUL.fullmatch(char) and not "a" <= char <= "z")
               or char.isdigit() for char in _TAG.sub("", _normalized(text)))


def _is_chorus(label: str) -> bool:
    return bool(_CHORUS_TAG.search(label)) and not bool(re.search(r"pre[-\s]*chorus", label, re.IGNORECASE))


def parse_lyric_sections(lyrics: str) -> list[dict[str, Any]]:
    """Keep every sung line verbatim; tags describe sections, never sung words.

    A written ``[Chorus x2]`` expands only the comparison/counting expectation.
    Numbered tags such as ``[Chorus 2]`` do not imply a repetition.
    """

    if not isinstance(lyrics, str):
        raise ValueError("lyrics must be text")
    if len(lyrics) > _MAX_TEXT_CHARS:
        raise ValueError("lyrics exceed the diagnostic text limit")
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] = {"label": "Unlabelled", "isChorus": False, "repeatCount": 1, "lines": []}
    for line in lyrics.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        cursor = 0
        for match in _TAG.finditer(line):
            before = line[cursor:match.start()]
            if before.strip():
                current["lines"].append(before)
            label = match.group(1).strip()
            if _normalized(label) not in _LANGUAGE_TAGS:
                if current["lines"] or current["label"] != "Unlabelled":
                    sections.append(current)
                repeat = _REPEAT.search(label)
                repetitions = int(next(value for value in repeat.groups() if value is not None)) if repeat else 1
                if not 1 <= repetitions <= 16:
                    raise ValueError("section repetition count must be between 1 and 16")
                current = {"label": label, "isChorus": _is_chorus(label),
                           "repeatCount": repetitions, "lines": []}
            cursor = match.end()
        after = line[cursor:]
        if after.strip():
            current["lines"].append(after)
    if current["lines"] or current["label"] != "Unlabelled":
        sections.append(current)
    return sections


def _english_syllables(word: str) -> int:
    """A coarse planning estimate, deliberately not a lyric rewriting operation."""

    word = word.replace("'", "")
    groups = len(re.findall(r"[aeiouy]+", word))
    if word.endswith("e") and not word.endswith(("le", "ye")) and groups > 1:
        groups -= 1
    return max(1, groups)


def _reference(lyrics: str) -> tuple[list[str], list[int], list[dict[str, Any]], list[dict[str, Any]]]:
    units: list[str] = []
    owners: list[int] = []
    occurrences = []
    lines = []
    for section_index, section in enumerate(parse_lyric_sections(lyrics)):
        section_lines = [(line.strip(), _units(line)) for line in section["lines"]]
        count = sum(len(line_units) for _, line_units in section_lines)
        for repeat in range(section["repeatCount"]):
            owner = len(occurrences)
            occurrences.append({"sectionIndex": section_index, "label": section["label"],
                                "occurrence": repeat + 1, "isChorus": section["isChorus"],
                                "expectedUnits": count})
            for text, line_units in section_lines:
                if line_units:
                    lines.append({"owner": owner, "text": text, "first": len(units), "units": len(line_units),
                                  "syllables": sum(1 if unit.startswith("ko:") else _english_syllables(unit[3:]) for unit in line_units)})
                units.extend(line_units)
            owners.extend([owner] * count)
    return units, owners, occurrences, lines


def assess_lyric_suitability(lyrics: str, duration_seconds: float, *, bpm: float | None = None) -> dict[str, Any]:
    """Estimate vocal pressure and suggest duration/planning without changing words."""

    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)) or not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("duration must be finite and positive")
    if bpm is not None and (isinstance(bpm, bool) or not isinstance(bpm, (int, float)) or not math.isfinite(bpm) or bpm <= 0):
        raise ValueError("bpm must be finite and positive")
    sections = parse_lyric_sections(lyrics)
    units, _, occurrences, _ = _reference(lyrics)
    korean = sum(item.startswith("ko:") for item in units)
    english = [item[3:] for item in units if item.startswith("en:")]
    syllables = korean + sum(_english_syllables(word) for word in english)
    unsupported = _unsupported_content(lyrics)
    instrumental = not units and any(_normalized(item["label"]) == "instrumental" for item in sections)
    language = "bilingual" if korean and english else "ko" if korean else "en" if english else "none"
    # Reserving some of the requested duration for arrangement is an estimate. Do
    # not infer exact vocal onset, insert silence, or slice syllables from this rate.
    vocal_fraction = 0.75
    rate = syllables / (duration_seconds * vocal_fraction)
    minimum = max(10, math.ceil(syllables / (4.0 * vocal_fraction) / 5) * 5) if syllables else None
    status = "not_applicable" if instrumental else "unknown" if not units or unsupported else "too_dense" if rate > 6.0 else "review" if rate > 4.0 else "suitable"
    reasons = []
    if status == "too_dense":
        reasons.append("lyrics_require_fast_delivery_for_requested_duration")
    elif status == "review":
        reasons.append("lyrics_may_need_more_vocal_time")
    elif status == "unknown":
        reasons.append("unsupported_lyric_content" if unsupported else "no_supported_korean_or_english_lyric_units")
    advice = ["preserve_all_lyric_words", "plan_complete_verses_and_each_written_chorus"]
    if status in {"too_dense", "review"}:
        advice.append("prefer_longer_duration_or_more_vocal_time_before_regenerating")
    return {
        "status": status, "language": language, "instrumental": instrumental,
        "unsupportedTextPresent": unsupported,
        "koreanSyllables": korean, "englishWords": len(english), "estimatedSungSyllables": syllables,
        "lineCount": sum(len(section["lines"]) * section["repeatCount"] for section in sections),
        "sections": sections, "sectionOccurrences": occurrences,
        "estimatedVocalSyllablesPerSecond": round(rate, 3),
        "suggestedMinimumDurationSeconds": minimum,
        "suggestedDurationWithinEngineLimit": minimum is None or minimum <= 600,
        "syllablesPerBeat": round(syllables / (duration_seconds * bpm / 60), 3) if bpm else None,
        "reasons": reasons, "planningAdvice": advice, "retryEligible": False,
        "heuristic": {"estimatedVocalFraction": vocal_fraction, "reviewRate": 4.0, "denseRate": 6.0},
    }


def _edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    row = list(range(len(hypothesis) + 1))
    for i, expected in enumerate(reference, 1):
        following = [i]
        for j, observed in enumerate(hypothesis, 1):
            following.append(min(row[j] + 1, following[-1] + 1, row[j - 1] + (expected != observed)))
        row = following
    return row[-1]


def _ordered_matches(reference: list[str], hypothesis: list[str]) -> list[tuple[int, int]]:
    """LCS alignment assigns each observed unit to at most one written occurrence."""

    rows = [array("H", [0]) * (len(hypothesis) + 1)]
    for expected in reversed(reference):
        previous = rows[-1]
        row = array("H", [0])
        for j, observed in enumerate(reversed(hypothesis), 1):
            row.append(previous[j - 1] + 1 if expected == observed else max(previous[j], row[j - 1]))
        rows.append(row)
    i, j = 0, 0
    matched = []
    while i < len(reference) and j < len(hypothesis):
        if reference[i] == hypothesis[j]:
            matched.append((i, j))
            i, j = i + 1, j + 1
        elif rows[len(reference) - i - 1][len(hypothesis) - j] > rows[len(reference) - i][len(hypothesis) - j - 1]:
            i += 1
        else:
            j += 1
    return matched


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _transcript(transcription: Any, duration_seconds: float | None) -> tuple[str, list[str], list[dict[str, Any]]]:
    reasons: list[str] = []
    accepted = []
    if transcription is None:
        return "", ["stt_unavailable"], []
    if isinstance(transcription, str):
        return transcription, ["stt_reliability_not_reported"], []
    if not isinstance(transcription, dict):
        return "", ["stt_result_invalid"], []
    if transcription.get("available") is False or transcription.get("error"):
        reasons.append("stt_unavailable")
    if transcription.get("task") == "translate":
        reasons.append("stt_translation_cannot_measure_original_lyrics")
    if transcription.get("reliable") is False:
        reasons.append("stt_reported_unreliable")
    segments = transcription.get("segments")
    text = transcription.get("text", "")
    if not isinstance(text, str):
        text = ""
        reasons.append("stt_text_invalid")
    if segments is None or segments == []:
        if transcription.get("reliable") is not True:
            reasons.append("stt_reliability_not_reported")
        return text, reasons, []
    if not isinstance(segments, list) or any(not isinstance(item, dict) for item in segments):
        return text, reasons + ["stt_segments_invalid"], []
    trusted = True
    for index, segment in enumerate(segments):
        segment_text = segment.get("text", "")
        if not isinstance(segment_text, str):
            reasons.append("stt_segment_text_invalid")
            continue
        start, end = _number(segment.get("start")), _number(segment.get("end"))
        if start is None or end is None or start < 0 or end <= start or (duration_seconds is not None and end > duration_seconds + 0.5):
            reasons.append("stt_timestamps_invalid")
            continue
        confidence = _number(segment.get("confidence"))
        logprob = _number(segment.get("avg_logprob"))
        no_speech = _number(segment.get("no_speech_prob"))
        if any(key in segment and _number(segment[key]) is None
               for key in ("confidence", "avg_logprob", "no_speech_prob")):
            reasons.append("stt_confidence_invalid")
        # Whisper's own failure thresholds: https://github.com/openai/whisper/blob/main/whisper/transcribe.py
        if confidence is not None and not 0 <= confidence <= 1:
            reasons.append("stt_confidence_invalid")
        if (confidence is not None and confidence < 0.5) or (logprob is not None and logprob < -1.0):
            reasons.append("stt_low_confidence")
        if no_speech is not None and (not 0 <= no_speech <= 1 or (no_speech > 0.6 and (logprob is None or logprob < -1.0))):
            reasons.append("stt_possible_silence_hallucination")
        if confidence is None and logprob is None:
            trusted = False
        accepted.append({"index": index, "start": start, "end": end, "text": segment_text})
    accepted.sort(key=lambda item: item["start"])
    if any(current["start"] < previous["end"] - 0.5 for previous, current in zip(accepted, accepted[1:])):
        reasons.append("stt_timestamps_overlap")
    if not trusted and transcription.get("reliable") is not True:
        reasons.append("stt_reliability_not_reported")
    joined = " ".join(item["text"] for item in accepted)
    if text.strip() and _units(text) != _units(joined):
        reasons.append("stt_text_and_segments_disagree")
    return joined, reasons, accepted


_MINIMUM_UNITS = 6             # about one full line; shorter stretches are not judged on their own
_LINE_UNHEARD_AT_MOST = 0.2    # share of a line's words matched; a stray common word is not the line
_SECTION_MISSING_BELOW = 0.25
# A whole section must be this long before its absence asks for another song; a missing
# line or two is named for the listener and left to a repaint of that stretch.
_SECTION_RETRY_MINIMUM_UNITS = 16
_MAX_REREAD_SECONDS = 90.0


def _measure(lyrics: str, transcription: dict[str, Any] | str | None, duration_seconds: float | None) -> dict[str, Any]:
    """Align one reading of the audio with the written lyrics; no verdict yet."""

    reference, owners, occurrences, lines = _reference(lyrics)
    text, reliability_reasons, segments = _transcript(transcription, duration_seconds)
    if _unsupported_content(lyrics):
        reliability_reasons.append("unsupported_reference_lyric_content")
    if _unsupported_content(text):
        reliability_reasons.append("stt_contains_unsupported_lyric_content")
    if len(text) > _MAX_TEXT_CHARS:
        reliability_reasons.append("stt_text_exceeds_diagnostic_limit")
        text = ""
    hypothesis = _units(text)
    if not hypothesis and reference:
        reliability_reasons.append("stt_has_no_usable_lyric_text")
    if not reference:
        reliability_reasons.append("no_supported_reference_lyric_units")
    if len(reference) * len(hypothesis) > _MAX_ALIGNMENT_CELLS:
        reliability_reasons.append("stt_alignment_exceeds_diagnostic_limit")
        hypothesis = []
    reference_counts, observed_counts = Counter(reference), Counter(hypothesis)
    if len(hypothesis) >= 40 and len(hypothesis) > max(3 * len(reference), len(reference) + 80):
        reliability_reasons.append("stt_excess_text_possible_hallucination")
    if any(count >= max(12, 3 * reference_counts[unit] + 6) for unit, count in observed_counts.items()) and len(hypothesis) > 2 * max(1, len(reference)):
        reliability_reasons.append("stt_repetition_possible_hallucination")
    normalized_reference = "".join(unit[3:] for unit in reference)
    normalized_observed = "".join(unit[3:] for unit in hypothesis)
    if any(phrase in normalized_observed and phrase not in normalized_reference for phrase in _HALLUCINATION_PHRASES):
        reliability_reasons.append("stt_boilerplate_possible_hallucination")
    if segments and any(len(_units(item["text"])) / (item["end"] - item["start"]) > 20 for item in segments):
        reliability_reasons.append("stt_text_rate_implausible")
    pairs = _ordered_matches(reference, hypothesis) if reference and hypothesis else []
    matched_counts = Counter(owners[index] for index, _ in pairs)
    sections = [{**item, "matchedUnits": matched_counts[index],
                 "orderedCoverage": matched_counts[index] / item["expectedUnits"] if item["expectedUnits"] and hypothesis else None}
                for index, item in enumerate(occurrences)]
    # Each observed unit belongs to the timestamped segment it was read in.
    unit_segment = [position for position, item in enumerate(segments) for _ in _units(item["text"])]
    unheard = []
    if segments and len(unit_segment) == len(hypothesis):
        heard = [0] * len(reference)
        for index, _ in pairs:
            heard[index] = 1
        onset = _number(transcription.get("vocalOnsetSeconds")) if isinstance(transcription, dict) else None
        end = duration_seconds if duration_seconds is not None else segments[-1]["end"]
        run: list[dict[str, Any]] = []
        for line in [*lines, None]:
            matched = sum(heard[line["first"]:line["first"] + line["units"]]) if line else 0
            if line and matched <= int(_LINE_UNHEARD_AT_MOST * line["units"]):
                run.append({**line, "matched": matched})
                continue
            if sum(item["units"] for item in run) >= _MINIMUM_UNITS:
                # Segment edges are coarse, so the stretch to read again includes both neighbours.
                first, last = run[0]["first"], run[-1]["first"] + run[-1]["units"] - 1
                before = [segments[unit_segment[j]] for i, j in pairs if i < first]
                after = [segments[unit_segment[j]] for i, j in pairs if i > last]
                outer = (before[-1]["start"] if before else 0.0, after[0]["end"] if after else end)
                inner = (before[-1]["end"] if before else max(0.0, (onset or 0.0) - 1.0), after[0]["start"] if after else end)
                listen = inner if inner[1] - inner[0] >= 1.0 else outer
                if outer[1] > outer[0]:
                    indexes = [position for position, item in enumerate(lines) if run[0]["first"] <= item["first"] <= run[-1]["first"]]
                    # The stretch also holds the sung line on each side, where there is one.
                    around = lines[max(0, indexes[0] - bool(before)):indexes[-1] + 1 + bool(after)]
                    unheard.append({"section": occurrences[run[0]["owner"]]["label"], "occurrence": occurrences[run[0]["owner"]]["occurrence"],
                                    "owners": sorted({item["owner"] for item in run}), "lineCount": len(run),
                                    "firstLine": run[0]["text"][:120], "lineIndexes": indexes,
                                    "syllables": sum(item["syllables"] for item in run), "spanSyllables": sum(item["syllables"] for item in around),
                                    "expectedUnits": sum(item["units"] for item in run), "matchedUnits": sum(item["matched"] for item in run),
                                    "searchSpanSeconds": [round(outer[0], 3), round(min(outer[1], outer[0] + _MAX_REREAD_SECONDS), 3)],
                                    "listenSpanSeconds": [round(listen[0], 3), round(listen[1], 3)]})
            run = []
    korean_ref = [item for item in reference if item.startswith("ko:")]
    english_ref = [item for item in reference if item.startswith("en:")]
    korean_observed = [item for item in hypothesis if item.startswith("ko:")]
    english_observed = [item for item in hypothesis if item.startswith("en:")]
    return {"reference": reference, "hypothesisUnits": len(hypothesis), "matchedUnits": len(pairs),
            "sections": sections, "lines": lines, "unheard": unheard, "segments": segments,
            "reliabilityReasons": sorted(set(reliability_reasons)),
            "known": bool(reference and hypothesis) and not reliability_reasons,
            "koreanReference": len(korean_ref), "englishReference": len(english_ref),
            "cer": _edit_distance(korean_ref, korean_observed) / len(korean_ref) if korean_ref and hypothesis else None,
            "wer": _edit_distance(english_ref, english_observed) / len(english_ref) if english_ref and hypothesis else None,
            "coverage": len(pairs) / len(reference) if reference and hypothesis else None,
            "anchor": _number(transcription.get("windowAnchorSeconds")) if isinstance(transcription, dict) else None}


def _with_reread(transcription: dict[str, Any], start: float, end: float, reread: dict[str, Any]) -> dict[str, Any]:
    """The same reading with one span replaced by another reading of that span."""

    def inside(segment: dict[str, Any]) -> bool:
        first, last = _number(segment.get("start")), _number(segment.get("end"))
        return first is not None and last is not None and start <= (first + last) / 2 <= end

    kept = [item for item in transcription.get("segments") or [] if isinstance(item, dict) and not inside(item)]
    added = [item for item in reread.get("segments") or [] if isinstance(item, dict) and inside(item)]
    merged = sorted(kept + added, key=lambda item: _number(item.get("start")) or 0.0)
    # The joined text is rebuilt from whichever segments pass the timestamp checks.
    return {**transcription, "segments": merged, "text": ""}


def evaluate_lyric_transcript(
    lyrics: str, transcription: dict[str, Any] | str | None, *, duration_seconds: float | None = None,
    rereads: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare an STT observation conservatively; unknown never authorizes a retry.

    A backend may attest ``reliable=True`` when its confidence checks are usable.
    Timestamped Whisper-style segments can establish this independently. Low
    confidence, impossible timestamps, and suspected hallucinations override that
    attestation. Repeating chorus text consumes distinct observed units.

    Written lines that went unheard in a row are listed in ``unheardLines`` with
    ``searchSpanSeconds``, the stretch of audio where they would be. ``rereads`` holds
    further readings of such stretches (``startSeconds``, ``endSeconds``,
    ``transcription``), each begun at another point so the recognizer's 30-second
    windows fall differently. A reading replaces the current one only where it matches
    more of the written words, which a recognizer that never saw the lyrics cannot do
    by chance. Lines are reported as missing, and a whole missing section as a reason
    to regenerate, only after two usable readings from other starting points still do
    not find them.
    """

    if duration_seconds is not None and (_number(duration_seconds) is None or duration_seconds <= 0):
        raise ValueError("duration must be finite and positive")
    measured = _measure(lyrics, transcription, duration_seconds)
    history = []
    reread_spans: list[tuple[float, float]] = []
    if measured["known"] and isinstance(transcription, dict):
        current = transcription
        for item in rereads or []:
            start, end, second = _number(item.get("startSeconds")), _number(item.get("endSeconds")), item.get("transcription")
            if start is None or end is None or end <= start or not isinstance(second, dict):
                continue
            usable = second.get("available") is not False and not second.get("error")
            # The same window from the same start reads the same thing; it confirms nothing.
            independent = end - start < 28.0 or measured["anchor"] is None or abs(start - measured["anchor"]) >= 1.0
            trial = _measure(lyrics, _with_reread(current, start, end, second), duration_seconds) if usable else None
            adopted = bool(trial and trial["known"] and trial["matchedUnits"] > measured["matchedUnits"])
            history.append({"startSeconds": start, "endSeconds": end, "usable": usable, "independent": independent,
                            "adopted": adopted, "matchedUnitsBefore": measured["matchedUnits"],
                            "matchedUnitsAfter": trial["matchedUnits"] if trial else None})
            if adopted:
                current, measured = _with_reread(current, start, end, second), trial
            # A reading that came back unreliable settles nothing about that span.
            if usable and independent and trial is not None and (trial["known"] or adopted):
                reread_spans.append((start, end))
    reference, sections = measured["reference"], measured["sections"]
    cer, wer, coverage, known = measured["cer"], measured["wer"], measured["coverage"], measured["known"]
    chorus = [item["orderedCoverage"] for item in sections
              if item["isChorus"] and item["expectedUnits"] and item["orderedCoverage"] is not None]
    retry_chorus = [item["orderedCoverage"] for item in sections
                    if item["isChorus"] and item["expectedUnits"] >= 8 and item["orderedCoverage"] is not None]
    retry_reasons = []
    # Retry only large, supported mismatches. Smaller errors remain observations
    # for listening because sung voice recognition itself can be wrong.
    if coverage is not None and coverage < 0.65 and len(reference) >= 20:
        retry_reasons.append("low_ordered_lyric_coverage")
    if cer is not None and cer > 0.65 and measured["koreanReference"] >= 20:
        retry_reasons.append("large_korean_transcript_error")
    if wer is not None and wer > 0.75 and measured["englishReference"] >= 6:
        retry_reasons.append("large_english_transcript_error")
    if retry_chorus and min(retry_chorus) < 0.5:
        retry_reasons.append("written_chorus_occurrence_missing")
    unheard = measured["unheard"] if known else []
    confirmed_lines: set[int] = set()
    for run in unheard:
        search = run["searchSpanSeconds"]
        again = sum(min(end, search[1]) - max(start, search[0]) >= 0.5 * (search[1] - search[0]) for start, end in reread_spans)
        run["rereadConfirmed"] = again >= 2
        if run["rereadConfirmed"]:
            confirmed_lines.update(run["lineIndexes"])
    # A whole written section is missing when it was barely matched and every one of
    # its lines is among those two further readings did not find.
    missing_sections = {index for index, item in enumerate(sections)
                        if item["expectedUnits"] >= _SECTION_RETRY_MINIMUM_UNITS and (item["orderedCoverage"] or 0) < _SECTION_MISSING_BELOW
                        and all(position in confirmed_lines for position, line in enumerate(measured["lines"]) if line["owner"] == index)}
    if any(not sections[index]["isChorus"] for index in missing_sections):
        retry_reasons.append("written_section_missing")
    findings = []
    for run in unheard:
        whole = bool(missing_sections.intersection(run["owners"]))
        listen = run["listenSpanSeconds"]
        findings.append({
            "check": "lyric_lines_missing" if run["rereadConfirmed"] else "lyric_lines_unclear", "severity": "warning",
            "retryEligible": whole, "confidence": 0.8 if run["rereadConfirmed"] else 0.5,
            "message": (f"{run['lineCount']} written line(s) of [{run['section']}] were not heard in three readings of this stretch."
                        if run["rereadConfirmed"] else
                        f"{run['lineCount']} written line(s) of [{run['section']}] were not recognized in this stretch."),
            "observed": {"section": run["section"], "occurrence": run["occurrence"], "lineCount": run["lineCount"],
                         "firstLine": run["firstLine"], "expectedUnits": run["expectedUnits"], "matchedUnits": run["matchedUnits"],
                         "wholeSection": whole, "furtherReadings": "still_not_heard" if run["rereadConfirmed"] else "not_available"},
            "threshold": {"lineMatchedShareAtMost": _LINE_UNHEARD_AT_MOST, "minimumExpectedUnits": _MINIMUM_UNITS,
                          "missingSectionCoverageBelow": _SECTION_MISSING_BELOW, "furtherReadingsRequired": 2},
            "startSeconds": listen[0], "endSeconds": listen[1]})
    retry_reasons = list(dict.fromkeys(retry_reasons))
    warning = (bool(retry_reasons) or bool(findings) or (cer is not None and cer > 0.35) or (wer is not None and wer > 0.45)
               or (coverage is not None and coverage < 0.8) or bool(chorus and min(chorus) < 0.8))
    instrumental = not reference and any(_normalized(item["label"]) == "instrumental" for item in parse_lyric_sections(lyrics))
    status = "not_applicable" if instrumental else "unknown" if not known else "warning" if warning else "pass"
    return {
        "status": status, "scope": "transcript_match_only", "sttReliable": known,
        "koreanCER": cer, "englishWER": wer, "orderedCoverage": coverage,
        "expectedUnits": len(reference), "observedUnits": measured["hypothesisUnits"], "matchedUnits": measured["matchedUnits"],
        "sectionCoverage": sections, "minimumChorusCoverage": min(chorus) if chorus else None,
        "unheardLines": [{key: value for key, value in run.items() if key != "lineIndexes"} for run in unheard],
        "segments": measured["segments"], "reliabilityReasons": measured["reliabilityReasons"],
        "findings": findings, "rereads": history,
        "retryEligible": known and bool(retry_reasons), "retryReasons": retry_reasons if known else [],
        "heuristicThresholds": {"retryCoverageBelow": 0.65, "retryKoreanCERAbove": 0.65,
                                "retryEnglishWERAbove": 0.75, "retryChorusCoverageBelow": 0.5,
                                "retrySectionCoverageBelow": _SECTION_MISSING_BELOW,
                                "retrySectionMinimumExpectedUnits": _SECTION_RETRY_MINIMUM_UNITS,
                                "unheardLinesMinimumExpectedUnits": _MINIMUM_UNITS},
    }


def reread_requests(report: dict[str, Any], *, limit: int = 3) -> list[tuple[float, float]]:
    """Stretches worth reading again, merged where they overlap, earliest first."""

    spans = sorted(tuple(item["searchSpanSeconds"]) for item in report.get("unheardLines", [])
                   if not item.get("rereadConfirmed"))
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] and end - merged[-1][0] <= _MAX_REREAD_SECONDS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged[:limit]


def reread_starts(start: float, end: float, *, anchor: float | None) -> list[float]:
    """Up to three points to begin reading one stretch from.

    A window that opens on an instrumental passage can fill with invented syllables and
    swallow the lines after it, so each reading opens somewhere else. The stretch's own
    start is skipped when the first pass already opened a full window there.
    """

    length = end - start
    starts = [start, start + min(7.5, length / 6), start + min(15.0, length / 3)]
    if anchor is not None and abs(start - anchor) < 1.0 and length >= 28.0:
        starts = starts[1:]
    return [round(value, 3) for value in starts if end - value >= 2.0]
