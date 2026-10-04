import math
import unicodedata

import pytest

from local_music_engine.lyric_quality import (
    assess_lyric_suitability, evaluate_lyric_transcript, parse_lyric_sections,
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
