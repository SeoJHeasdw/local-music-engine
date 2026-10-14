import math
import unicodedata

import pytest

from local_music_engine.lyric_quality import (
    assess_lyric_suitability, evaluate_lyric_transcript, parse_lyric_sections, reread_requests, reread_starts,
)


def trustworthy(text, **kwargs):
    return {"text": text, "reliable": True, **kwargs}


def test_preflight_preserves_sung_lines_and_counts_bilingual_sections():
    lyrics = "[Verse 1]\r\n  꿈을 따라 가  \r\nShine on me!\r\n[Pre-Chorus]\r\n가자\r\n[Chorus x2]\r\n우리 함께 걸어가\r\n[Chorus 2]\r\n꿈을 꾸자"
    sections = parse_lyric_sections(lyrics)
    assert sections[0]["lines"] == ["  꿈을 따라 가  ", "Shine on me!"]
    assert not sections[1]["isChorus"]
    assert sections[2]["isChorus"] and sections[2]["repeatCount"] == 2
    assert sections[3]["repeatCount"] == 1
    result = assess_lyric_suitability(lyrics, 60, bpm=100)
    assert result["language"] == "bilingual"
    assert result["koreanSyllables"] == 5 + 2 + 2 * 7 + 4
    assert result["englishWords"] == 3
    assert result["lineCount"] == 6
    assert result["status"] == "suitable" and not result["retryEligible"]
    assert len(result["sectionOccurrences"]) == 5
    assert result["sections"] == sections
    assert "꿈을 따라 가" in lyrics  # no reconstructed lyrics are applied


def test_density_advice_changes_planning_only_and_does_not_silently_cap_needed_duration():
    lyrics = "[Verse]\n" + "가나다라" * 100
    result = assess_lyric_suitability(lyrics, 30)
    assert result["status"] == "too_dense"
    assert result["suggestedMinimumDurationSeconds"] >= 130
    assert "preserve_all_lyric_words" in result["planningAdvice"]
    assert "lyrics" not in result and not result["retryEligible"]
    enormous = assess_lyric_suitability("가" * 2_000, 60)
    assert enormous["suggestedMinimumDurationSeconds"] > 600
    assert not enormous["suggestedDurationWithinEngineLimit"]


def test_instrumental_and_unsupported_lyrics_do_not_claim_vocal_success():
    assert assess_lyric_suitability("[Instrumental]", 30)["status"] == "not_applicable"
    assert evaluate_lyric_transcript("[Instrumental]", None)["status"] == "not_applicable"
    result = evaluate_lyric_transcript("[Verse]\n春の歌", trustworthy("spring song"))
    assert result["status"] == "unknown" and not result["retryEligible"]


@pytest.mark.parametrize("lyrics", ["가사 123", "가사 春の歌", "sing café"])
def test_unsupported_sung_content_is_not_silently_omitted_from_quality_assessment(lyrics):
    planning = assess_lyric_suitability(lyrics, 30)
    assert planning["status"] == "unknown" and planning["unsupportedTextPresent"]
    result = evaluate_lyric_transcript(lyrics, trustworthy(lyrics))
    assert result["status"] == "unknown" and not result["retryEligible"]
    assert "unsupported_reference_lyric_content" in result["reliabilityReasons"]


@pytest.mark.parametrize("value", [0, -1, True, math.nan, math.inf, "30"])
def test_preflight_rejects_invalid_duration(value):
    with pytest.raises(ValueError):
        assess_lyric_suitability("가사", value)


def test_korean_cer_ignores_spacing_tags_punctuation_and_unicode_composition():
    lyrics = "[Verse]\n내 마음을 따라\n[Chorus]\n우리 함께 걸어가"
    observed = unicodedata.normalize("NFD", "[ko] 내마음을따라, 우리함께걸어가!")
    result = evaluate_lyric_transcript(lyrics, trustworthy(observed))
    assert result["status"] == "pass"
    assert result["koreanCER"] == 0 and result["englishWER"] is None
    assert result["orderedCoverage"] == 1 and not result["retryEligible"]


def test_english_wer_ignores_case_punctuation_contraction_apostrophe_and_tags():
    lyrics = "[Verse]\nDon't Stop, SHINE on me.\n[Chorus]\nWe'll run away!"
    result = evaluate_lyric_transcript(lyrics, trustworthy("[en] don’t stop shine on me we'll RUN AWAY"))
    assert result["status"] == "pass"
    assert result["englishWER"] == 0 and result["koreanCER"] is None


def test_bilingual_metrics_measure_each_language_without_penalizing_the_other():
    result = evaluate_lyric_transcript("가나다라\nshine on me", trustworthy("가나마라 shine on you"))
    assert result["koreanCER"] == pytest.approx(0.25)
    assert result["englishWER"] == pytest.approx(1 / 3)
    assert result["orderedCoverage"] == pytest.approx(5 / 7)
    assert not result["retryEligible"]  # short observations do not justify regeneration


def test_edit_rate_can_exceed_one_instead_of_hiding_insertions():
    result = evaluate_lyric_transcript("shine", trustworthy("shine on me forever and ever"))
    assert result["englishWER"] == 5
    assert result["status"] == "warning" and not result["retryEligible"]


@pytest.mark.parametrize("transcription", [None, "가사", {}, {"available": False}, {"reliable": False, "text": "가사"},
                                            {"reliable": True, "text": ""}, {"error": "checker failed", "text": "가사", "reliable": True}])
def test_absent_or_unreliable_stt_is_unknown_never_pass_or_retry(transcription):
    result = evaluate_lyric_transcript("[Verse]\n가사", transcription)
    assert result["status"] == "unknown"
    assert not result["sttReliable"] and not result["retryEligible"]
    assert result["retryReasons"] == [] and result["reliabilityReasons"]
    if transcription is None:
        assert result["koreanCER"] is None and result["orderedCoverage"] is None


def test_one_recognized_chorus_cannot_cover_two_written_occurrences():
    verse = "조용한 아침 햇살 따라 걸어가"
    chorus = "너와 함께 새로운 꿈을 향해 가"
    lyrics = f"[Verse]\n{verse}\n[Chorus]\n{chorus}\n[Chorus]\n{chorus}"
    result = evaluate_lyric_transcript(lyrics, trustworthy(verse + " " + chorus))
    chorus_rows = [row for row in result["sectionCoverage"] if row["isChorus"]]
    assert len(chorus_rows) == 2
    assert sum(row["matchedUnits"] for row in chorus_rows) <= len(chorus.replace(" ", ""))
    assert result["minimumChorusCoverage"] < 0.5
    assert result["retryEligible"] and "written_chorus_occurrence_missing" in result["retryReasons"]
    complete = evaluate_lyric_transcript(lyrics, trustworthy(verse + " " + chorus + " " + chorus))
    assert complete["status"] == "pass" and complete["minimumChorusCoverage"] == 1


def test_explicit_chorus_repeat_is_counted_but_verse_chorus_order_is_not_ignored():
    lyrics = "[Verse]\nred blue green yellow orange purple silver gold\n[Chorus x2]\nwe walk together into another bright new morning"
    one = evaluate_lyric_transcript(lyrics, trustworthy("red blue green yellow orange purple silver gold we walk together into another bright new morning"))
    assert one["retryEligible"]
    reversed_result = evaluate_lyric_transcript("[Verse]\nred blue green yellow\n[Chorus]\nwe walk into morning",
                                              trustworthy("we walk into morning red blue green yellow"))
    assert reversed_result["orderedCoverage"] == 0.5


def test_short_missing_chorus_still_warns_without_overconfident_retry():
    verse = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty"
    result = evaluate_lyric_transcript(f"[Verse]\n{verse}\n[Chorus]\nshine together", trustworthy(verse))
    assert result["status"] == "warning" and result["minimumChorusCoverage"] == 0
    assert not result["retryEligible"]


def test_whisper_segments_support_reliability_and_are_ordered_by_timestamps():
    text = "내 마음을 따라 우리 함께 걸어가"
    segments = [{"start": 8, "end": 14, "text": "우리 함께 걸어가", "avg_logprob": -0.2, "no_speech_prob": 0.1},
                {"start": 1, "end": 6, "text": "내 마음을 따라", "avg_logprob": -0.1, "no_speech_prob": 0.1}]
    result = evaluate_lyric_transcript(text, {"text": text, "segments": segments}, duration_seconds=20)
    assert result["status"] == "pass" and result["sttReliable"]
    assert [segment["start"] for segment in result["segments"]] == [1, 8]
    assert result["orderedCoverage"] == 1


@pytest.mark.parametrize("patch, reason", [
    ({"start": -1}, "stt_timestamps_invalid"),
    ({"end": 40}, "stt_timestamps_invalid"),
    ({"end": 1}, "stt_timestamps_invalid"),
    ({"confidence": 0.1}, "stt_low_confidence"),
    ({"avg_logprob": -2}, "stt_low_confidence"),
    ({"avg_logprob": -2, "no_speech_prob": 0.9}, "stt_possible_silence_hallucination"),
    ({"confidence": math.nan}, "stt_confidence_invalid"),
])
def test_bad_segments_override_backend_reliability_claim(patch, reason):
    segment = {"start": 1, "end": 8, "text": "꿈을 따라 걸어가", "confidence": 0.9, **patch}
    result = evaluate_lyric_transcript("꿈을 따라 걸어가", trustworthy(segment["text"], segments=[segment]), duration_seconds=20)
    assert result["status"] == "unknown" and reason in result["reliabilityReasons"]
    assert not result["retryEligible"]


def test_translated_stt_and_segment_disagreement_are_unknown():
    translated = evaluate_lyric_transcript("꿈을 따라", trustworthy("follow the dream", task="translate"))
    assert translated["status"] == "unknown"
    disagreement = evaluate_lyric_transcript("꿈을 따라", trustworthy("꿈을 따라", segments=[{"start": 1, "end": 3, "text": "다른 가사"}]))
    assert disagreement["status"] == "unknown"
    assert "stt_text_and_segments_disagree" in disagreement["reliabilityReasons"]


def test_hallucination_boilerplate_is_unknown_unless_written_in_the_lyrics():
    result = evaluate_lyric_transcript("햇살 따라 걸어가", trustworthy("thanks for watching"))
    assert result["status"] == "unknown" and "stt_boilerplate_possible_hallucination" in result["reliabilityReasons"]
    legitimate = evaluate_lyric_transcript("Thanks for watching", trustworthy("thanks for watching"))
    assert legitimate["status"] == "pass"


def test_hallucinated_repetition_is_not_a_generation_retry_reason():
    result = evaluate_lyric_transcript("[Verse]\n우리 함께 꿈을 따라", trustworthy("우리 함께 " * 60))
    assert result["status"] == "unknown" and not result["retryEligible"]
    assert "stt_repetition_possible_hallucination" in result["reliabilityReasons"]


def test_large_confident_mismatch_can_retry_without_rewriting_lyrics():
    lyrics = "[Verse]\n우리는 새로운 아침 햇살을 따라 노래하고 웃으며 함께 걸어가"
    result = evaluate_lyric_transcript(lyrics, trustworthy("다른 도시 어두운 바람 속에서 낯선 사람들을 만나 이야기를 나누네"))
    assert result["status"] == "warning" and result["retryEligible"]
    assert "low_ordered_lyric_coverage" in result["retryReasons"]
    assert "lyrics" not in result


def test_alignment_and_text_limits_return_unknown_instead_of_unbounded_work():
    result = evaluate_lyric_transcript("가" * 2_000, trustworthy("나" * 2_000))
    assert result["status"] == "unknown" and not result["retryEligible"]
    assert "stt_alignment_exceeds_diagnostic_limit" in result["reliabilityReasons"]
    too_long = evaluate_lyric_transcript("가사", trustworthy("나" * 24_001))
    assert too_long["status"] == "unknown" and too_long["koreanCER"] is None


VERSE = ("silver morning carries quiet rivers\nunder open skies tonight we wander\n"
         "every corner hums a little louder\nfootsteps keeping time with distant thunder")
BRIDGE = "paper lanterns drifting over sleeping harbour\nwater slowly turning golden again"
CHORUS = "hold me closer when the neon fades away\nand never let the midnight take our song\nwe belong to every colour of the dawn"
SONG = f"[Verse]\n{VERSE}\n[Chorus]\n{CHORUS}\n[Bridge]\n{BRIDGE}\n[Chorus]\n{CHORUS}"


def heard(*lines, anchor=0.0):
    """Whisper-style segments: (start, end, text)."""
    return {"available": True, "reliable": True, "windowAnchorSeconds": anchor, "text": " ".join(text for _, _, text in lines),
            "segments": [{"start": start, "end": end, "text": text, "avg_logprob": -0.2, "no_speech_prob": 0.01}
                         for start, end, text in lines]}


def again(start, end, *lines):
    return {"startSeconds": start, "endSeconds": end, "transcription": heard(*lines)}


FIRST_READING = heard((8, 14, "oh oh oh"), (30, 40, CHORUS), (40, 48, BRIDGE), (48, 58, CHORUS))


def test_a_whole_missing_verse_is_not_hidden_by_a_well_sung_chorus():
    result = evaluate_lyric_transcript(SONG, FIRST_READING, duration_seconds=60)
    assert result["orderedCoverage"] > 0.65 and result["englishWER"] < 0.75  # the totals alone ask for nothing
    assert result["sectionCoverage"][0]["orderedCoverage"] == 0 and result["status"] == "warning"
    # One reading is not proof: the place is named for the listener, nothing is regenerated.
    [finding] = result["findings"]
    assert (finding["check"], finding["retryEligible"], finding["startSeconds"], finding["endSeconds"]) == ("lyric_lines_unclear", False, 0.0, 30.0)
    assert finding["observed"]["lineCount"] == 4 and finding["observed"]["firstLine"] == "silver morning carries quiet rivers"
    assert result["retryReasons"] == [] and reread_requests(result) == [(0.0, 40.0)]


def test_another_reading_that_hears_the_verse_replaces_the_first():
    result = evaluate_lyric_transcript(SONG, FIRST_READING, duration_seconds=60,
                                       rereads=[again(6.0, 40.0, (7.5, 14, VERSE), (30, 40, CHORUS))])
    assert result["status"] == "pass" and result["findings"] == [] and result["retryReasons"] == []
    assert result["sectionCoverage"][0]["orderedCoverage"] == 1 and result["unheardLines"] == []
    assert result["rereads"][0]["adopted"] and result["rereads"][0]["matchedUnitsAfter"] > result["rereads"][0]["matchedUnitsBefore"]
    assert [segment["text"] for segment in result["segments"]][:2] == [VERSE, CHORUS]


def test_a_verse_no_reading_finds_is_missing_and_may_regenerate():
    twice = [again(6.0, 40.0, (30, 40, CHORUS)), again(12.0, 40.0, (30, 40, CHORUS))]
    once = evaluate_lyric_transcript(SONG, FIRST_READING, duration_seconds=60, rereads=twice[:1])
    assert once["findings"][0]["check"] == "lyric_lines_unclear" and once["retryReasons"] == []  # one more reading is not enough
    result = evaluate_lyric_transcript(SONG, FIRST_READING, duration_seconds=60, rereads=twice)
    [finding] = result["findings"]
    assert finding["check"] == "lyric_lines_missing" and finding["retryEligible"] and finding["observed"]["wholeSection"]
    assert finding["observed"]["furtherReadings"] == "still_not_heard" and result["unheardLines"][0]["rereadConfirmed"]
    assert result["retryReasons"] == ["written_section_missing"] and result["retryEligible"]
    assert reread_requests(result) == []  # nothing left to read again


@pytest.mark.parametrize("second", [
    {"available": False, "error": "recognizer unavailable", "segments": []},
    # Low recognizer confidence in another reading settles nothing.
    {"available": True, "segments": [{"start": 8, "end": 14, "text": "la la", "avg_logprob": -2.0, "no_speech_prob": 0.9}]},
])
def test_unusable_further_readings_confirm_nothing(second):
    rereads = [{"startSeconds": start, "endSeconds": 40.0, "transcription": second} for start in (6.0, 12.0)]
    result = evaluate_lyric_transcript(SONG, FIRST_READING, duration_seconds=60, rereads=rereads)
    assert result["findings"][0]["check"] == "lyric_lines_unclear" and result["retryReasons"] == []
    assert result["status"] == "warning" and not any(item["adopted"] for item in result["rereads"])


def test_the_same_window_read_again_is_not_another_opinion():
    same = [again(0.0, 40.0, (30, 40, CHORUS)), again(0.5, 40.0, (30, 40, CHORUS))]
    result = evaluate_lyric_transcript(SONG, FIRST_READING, duration_seconds=60, rereads=same)
    assert [item["independent"] for item in result["rereads"]] == [False, False]
    assert result["findings"][0]["check"] == "lyric_lines_unclear" and "written_section_missing" not in result["retryReasons"]
    assert reread_starts(0.0, 40.0, anchor=0.0) == [6.667, 13.333]  # so the first pass's own window is not asked for again
    assert reread_starts(12.0, 30.0, anchor=0.0) == [12.0, 15.0, 18.0]
    assert reread_starts(57.0, 60.0, anchor=0.0) == [57.0, 57.5, 58.0] and reread_starts(59.0, 60.0, anchor=0.0) == []


def test_two_unsung_lines_are_named_without_calling_the_section_missing():
    lines = VERSE.split("\n")
    sung = "\n".join(lines[:2])  # the verse's second pair of lines was never sung
    rest = (16, 24, CHORUS), (24, 30, BRIDGE), (30, 40, CHORUS)
    result = evaluate_lyric_transcript(SONG, heard((0, 16, sung), *rest), duration_seconds=45,
                                       rereads=[again(3.0, 24.0, (3, 16, sung), rest[0]), again(6.0, 24.0, (6, 16, lines[1]), rest[0])])
    [finding] = result["findings"]
    assert finding["check"] == "lyric_lines_missing" and not finding["retryEligible"]
    assert finding["observed"]["lineCount"] == 2 and finding["observed"]["firstLine"] == lines[2]
    assert not finding["observed"]["wholeSection"] and 0.4 < result["sectionCoverage"][0]["orderedCoverage"] < 0.6
    assert result["retryReasons"] == [] and result["status"] == "warning"


def test_a_short_missing_section_is_a_place_to_repair_not_a_reason_for_another_song():
    lyrics = f"[Verse]\n{VERSE}\n[Chorus]\n{CHORUS}\n[Outro]\nlet me stay till the neon fades"
    reading = heard((0, 16, VERSE), (16, 26, CHORUS))
    rereads = [again(start, 30.0, (16, 26, CHORUS)) for start in (16.0, 18.0, 20.0)]
    result = evaluate_lyric_transcript(lyrics, reading, duration_seconds=30, rereads=rereads)
    [finding] = result["findings"]
    assert (finding["check"], finding["observed"]["section"], finding["observed"]["lineCount"]) == ("lyric_lines_missing", "Outro", 1)
    assert result["sectionCoverage"][2]["orderedCoverage"] == 0 and result["retryReasons"] == [] and not finding["retryEligible"]


def test_a_garbled_but_sung_section_and_a_short_tag_line_are_not_called_unheard():
    garbled = ("silver morning carried quite reverse\nunder open sky tonight be wonder\n"
               "every corner comes a little lower\nfootstep keeping time with this and under")  # misheard words, every line present
    result = evaluate_lyric_transcript(SONG, heard((5, 12, garbled), (12, 24, CHORUS), (24, 30, BRIDGE), (30, 42, CHORUS)), duration_seconds=45)
    assert result["findings"] == [] and result["sectionCoverage"][0]["orderedCoverage"] < 0.7
    short = evaluate_lyric_transcript(f"[Verse]\n{VERSE}\n[Outro]\nfades away", heard((5, 12, VERSE)), duration_seconds=20)
    assert short["findings"] == [] and short["status"] == "pass"


def test_overlapping_stretches_are_read_once():
    result = evaluate_lyric_transcript(SONG, heard((30, 40, CHORUS), (48, 58, CHORUS)), duration_seconds=60)
    assert [item["observed"]["section"] for item in result["findings"]] == ["Verse", "Bridge"]
    assert reread_requests(result) == [(0.0, 58.0)]
