"""Music 3 defaults, immutable provenance and unsupported operation boundaries."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from local_music_engine import ace_adapter
from local_music_engine.auto_quality import prepare_payload
from local_music_engine.cli import build_parser, run
from local_music_engine.drafting import draft_song
from local_music_engine.music3 import BASE_URL, CAPABILITIES, ENGINE, MAX_DURATION_SECONDS, MODEL, plain_lyrics
from local_music_engine.music3_adapter import Music3Client
from local_music_engine.production_rules import RULES
from local_music_engine.storage import ProjectStore
from local_music_engine.views import project_status
from local_music_engine.workflow import (
    _api_payload, _frozen_generation_payload, cover_candidates, generate_candidates,
    repaint_candidate, resume_latest_batch,
)
from test_workflow import FakeAceClient, make_project


class FakeMusic3Client(FakeAceClient):
    payloads: list[dict] = []
    loaded_model = MODEL
    engine = ENGINE
    models_initialized = True

    def health(self):
        return {"status": "ok", "engine": self.engine, "models_initialized": self.models_initialized,
                "loaded_model": self.loaded_model, "loaded_lm_model": None, "llm_initialized": True,
                "realInference": False, "capabilities": CAPABILITIES, "max_duration_seconds": 300}

    def submit(self, request, source_audio=None):
        assert source_audio is None
        self.payloads.append(deepcopy(request))
        return super().submit(request)


@pytest.fixture(autouse=True)
def reset_mock():
    FakeMusic3Client.payloads = []
    FakeMusic3Client.submitted = []
    FakeMusic3Client.fail_seeds = set()
    FakeMusic3Client.engine = ENGINE
    FakeMusic3Client.loaded_model = MODEL
    FakeMusic3Client.models_initialized = True


def test_new_generation_defaults_to_music3_and_keeps_human_review_separate(tmp_path):
    make_project(tmp_path)
    result = generate_candidates(tmp_path, seeds=[9], client_factory=FakeMusic3Client)
    saved = ProjectStore(tmp_path).load()
    request = saved["requests"][0]
    assert result["status"] == "succeeded"
    assert request["adapter"] == "minimax-music3-mlx"
    assert request["parameters"]["engine"] == ENGINE
    assert request["parameters"]["model"] == MODEL
    assert request["executionKind"] == "unverified-transport"
    assert saved["jobs"][0]["parameters"]["baseUrl"] == BASE_URL
    assert set(FakeMusic3Client.payloads[0]) == {"prompt", "lyrics", "audio_duration", "seed", "model", "batch_size", "inference_steps", "task_type"}
    assert FakeMusic3Client.payloads[0]["inference_steps"] == 30
    assert saved["candidates"][0]["humanReview"]["status"] == "unreviewed"
    assert saved["selectedCandidateId"] is None


@pytest.mark.parametrize("mismatch", ["model", "engine", "readiness"])
def test_music3_refuses_wrong_or_unready_server_before_submission(tmp_path, mismatch):
    make_project(tmp_path)
    if mismatch == "model": FakeMusic3Client.loaded_model = "another-checkpoint"
    if mismatch == "engine": FakeMusic3Client.engine = "ace-step"
    if mismatch == "readiness": FakeMusic3Client.models_initialized = False
    with pytest.raises(Exception, match="Music 3 server"):
        generate_candidates(tmp_path, seeds=[1], client_factory=FakeMusic3Client)
    saved = ProjectStore(tmp_path).load()
    assert saved["requests"] == saved["candidates"] == FakeMusic3Client.payloads == []
    assert saved["jobs"][0]["status"] == "failed"


def test_music3_maps_every_rule_to_correct_caption_section_and_plain_lyrics(tmp_path):
    lyrics = "[Verse]\nCity lights are fading\nYour voice stays with me\n\n[Chorus]\nWe can take it slow"
    selection = {"version": 1, "presetId": "emotional-hiphop", "ruleIds": [r["id"] for r in RULES]}
    store = ProjectStore.initialize(tmp_path, title="Night", lyrics=lyrics, style_prompt="warm male vocal",
                                   target_duration_seconds=30, production_rules=selection)
    frozen = _frozen_generation_payload(store.load(), seed=1, model=MODEL, lm_model="unused")
    metadata, rest = frozen["prompt"].split("[Vocal Details]")
    vocals, arrangement = rest.split("[Arrangement]")
    assert "Emotional melodic hip hop" in metadata
    assert "One clear lead vocal" in vocals and "natural breath between lines" in vocals
    assert "Spacious drums, bass and one main chord instrument" in arrangement
    for rule in frozen["music3ProductionGuidance"]["rules"]:
        assert rule["caption"] in frozen["prompt"] and rule["native"] is True
    assert "emotionally connected lead" in vocals and "memorable short sung motif" in arrangement
    assert frozen["sourceLyricsOriginal"] == lyrics
    assert frozen["lyrics"].splitlines() == lyrics.splitlines()
    assert "Verse: restrained" in arrangement and "Chorus: fuller" in arrangement
    assert "[Verse - restrained]" not in frozen["lyrics"]
    assert "thinking" not in frozen and "lm_model_path" not in _api_payload(frozen)
    assert store.load()["inputs"]["lyricsOriginal"] == lyrics


@pytest.mark.parametrize("lyrics", ["[instrumental]", "[INSTRUMENTAL]", " \n[InStRuMeNtAl]\n "])
def test_manual_instrumental_case_keeps_words_and_skips_vocal_production(tmp_path, lyrics):
    from local_music_engine.views import library

    selection = {"version": 1, "presetId": "emotional-hiphop", "ruleIds": [rule["id"] for rule in RULES]}
    store = ProjectStore.initialize(tmp_path / "song", title="Instrumental", lyrics=lyrics,
                                   style_prompt="warm piano hip hop", target_duration_seconds=30,
                                   production_rules=selection)
    original = store.load()
    frozen = _frozen_generation_payload(original, seed=1, model=MODEL, lm_model="unused")
    prepared = prepare_payload(frozen)

    assert frozen["lyrics"] == prepared["lyrics"] == lyrics
    assert prepared["sourceLyricsOriginal"] == lyrics
    assert prepared["qualityPreparation"]["lyricsOriginal"] == lyrics
    assert "outro_structure_tag_added" not in prepared["qualityPreparation"]["changes"]
    assert prepared["songPlan"]["options"]["instrumental"] is True
    assert set(prepared["productionRules"]["skippedRuleIds"]) == {
        "clear-vocal", "phrase-breathing", "expressive-performance",
    }
    assert "Instrumental; no vocals." in prepared["prompt"]
    assert "fully sung chorus" not in prepared["prompt"]
    assert "lead vocal" not in prepared["prompt"]
    assert "short sung motif" not in prepared["prompt"]
    assert library(tmp_path)[0]["instrumental"] is True
    assert store.load() == original


@pytest.mark.parametrize("lyrics", ["[instrumental]\nStay with me", "[Verse]\nInstrumental", ""])
def test_instrumental_mode_does_not_discard_sung_text(lyrics):
    from local_music_engine.lyrics import is_instrumental_lyrics

    assert is_instrumental_lyrics(lyrics) is False


def test_manual_section_directions_are_not_sung_or_lost(tmp_path):
    lyrics = "[Verse 2 - whispered]\n너와 함께 걸어\n\n[Chorus: fuller]\nStay with me"
    result, instructions = plain_lyrics(lyrics)
    assert result == "[Verse 2]\n너와 함께 걸어\n\n[Chorus]\nStay with me"
    assert instructions == ["Verse 2: whispered.", "Chorus: fuller."]
    assert plain_lyrics("[User's custom section]\nSame words")[0] == "[User's custom section]\nSame words"


def test_quality_preparation_keeps_music3_caption_and_duration_limit(tmp_path):
    store = ProjectStore.initialize(tmp_path, title="Dense", lyrics="[Verse]\n" + "마음이 달려가 " * 500,
                                   style_prompt="ballad", target_duration_seconds=30)
    frozen = _frozen_generation_payload(store.load(), seed=1, model=MODEL, lm_model="unused")
    prepared = prepare_payload(frozen)
    assert prepared["audio_duration"] <= MAX_DURATION_SECONDS
    assert f"Duration: {prepared['audio_duration']:g} seconds." in prepared["prompt"]
    assert prepared["prompt"].count("[Global Metadata]") == 1
    assert prepared["qualityPreparation"]["requestedDurationSeconds"] == 30


def test_eos_short_result_is_recorded_without_padding_or_false_duration(tmp_path):
    store = ProjectStore.initialize(tmp_path, title="EOS", lyrics="[Verse]\nStay with me",
                                   style_prompt="hip hop", target_duration_seconds=30)
    generate_candidates(tmp_path, seeds=[1], client_factory=FakeMusic3Client)
    saved = store.load(); candidate = saved["candidates"][0]
    assert candidate["requestedDurationSeconds"] == 30
    assert candidate["actualDurationSeconds"] == 10
    assert any(f["check"] == "duration" and f["observed"]["actualSeconds"] == 10 for f in saved["findings"])
    assert saved["artifacts"][0]["audio"]["durationSeconds"] == 10


def test_legacy_resume_requires_explicit_engine_and_preserves_frozen_records(tmp_path):
    make_project(tmp_path)
    FakeAceClient.fail_seeds = {2}; FakeAceClient.submitted = []
    old = generate_candidates(tmp_path, seeds=[1, 2], client_factory=FakeAceClient, engine="ace-step")
    before = ProjectStore(tmp_path).load(); requests = deepcopy(before["requests"])
    with pytest.raises(ValueError, match="legacy ACE"):
        resume_latest_batch(tmp_path, client_factory=FakeMusic3Client)
    assert ProjectStore(tmp_path).load() == before
    status = project_status(ProjectStore(tmp_path), before)
    assert status["jobs"][0]["canResume"] is False and "ACE" in status["jobs"][0]["resumeBlockedReason"]
    FakeAceClient.fail_seeds = set()
    resumed = resume_latest_batch(tmp_path, client_factory=FakeAceClient, engine="ace-step")
    assert resumed["reusedCandidateIds"] == old["candidateIds"]
    assert ProjectStore(tmp_path).load()["requests"][:len(requests)] == requests


def test_music3_regeneration_reuses_legacy_text_without_source_audio(tmp_path):
    make_project(tmp_path)
    FakeAceClient.fail_seeds = set()
    old = generate_candidates(tmp_path, seeds=[1], client_factory=FakeAceClient, engine="ace-step")
    original = deepcopy(ProjectStore(tmp_path).load()["requests"][0])
    generate_candidates(tmp_path, seeds=[2], source_candidate_id=old["candidateIds"][0], client_factory=FakeMusic3Client)
    saved = ProjectStore(tmp_path).load()
    assert saved["requests"][0] == original
    assert saved["requests"][1]["parameters"]["lyrics"] == original["parameters"]["lyrics"]
    assert saved["requests"][1]["parameters"]["sourceStylePrompt"] == "Korean pop"
    assert saved["requests"][1]["parameters"]["engine"] == ENGINE
    assert saved["selectedCandidateId"] is None


def test_music3_resume_uses_only_matching_verified_frozen_request(tmp_path):
    make_project(tmp_path)
    FakeMusic3Client.fail_seeds = {2}
    first = generate_candidates(tmp_path, seeds=[1, 2], client_factory=FakeMusic3Client)
    first_requests = deepcopy(ProjectStore(tmp_path).load()["requests"])
    FakeMusic3Client.fail_seeds = set()
    result = resume_latest_batch(tmp_path, client_factory=FakeMusic3Client)
    assert result["reusedCandidateIds"] == first["candidateIds"]
    assert FakeMusic3Client.submitted == [1, 2, 2]
    assert ProjectStore(tmp_path).load()["requests"][:len(first_requests)] == first_requests


def test_unsupported_music3_edits_reject_before_manifest_mutation(tmp_path):
    make_project(tmp_path); before = ProjectStore(tmp_path).load()
    with pytest.raises(ValueError, match="does not support cover"):
        cover_candidates(tmp_path, candidate_id="old-id", seeds=[1])
    with pytest.raises(ValueError, match="does not support repaint"):
        repaint_candidate(tmp_path, start_seconds=1, end_seconds=2, seed=1)
    assert ProjectStore(tmp_path).load() == before


def test_draft_rules_work_with_engine_off_and_disclose_template_origin():
    def forbidden_client(*args, **kwargs): raise AssertionError("draft must not contact music server")
    for language in ("ko", "en"):
        draft = draft_song("신나는 팝", instrumental=False, duration_seconds=60, base_url=BASE_URL,
                           vocal_language=language, client_factory=forbidden_client)
        assert draft["source"] == "rules" and draft["sourceModel"] is None
        assert draft["lyrics"] and "예시" in draft["notes"][0]
        assert draft["vocalLanguage"] == language


def test_cli_default_contract_and_capabilities():
    parser = build_parser()
    args = parser.parse_args(["generate", "song", "--seeds", "1"])
    assert (args.engine, args.model, args.base_url) == (ENGINE, MODEL, BASE_URL)
    assert run(parser.parse_args(["capabilities"])) == {"engine": ENGINE, "model": MODEL, "capabilities": CAPABILITIES, "maxDurationSeconds": 300}


def test_music3_transport_uses_own_late_published_private_credential(tmp_path, monkeypatch):
    key = tmp_path / "key"
    monkeypatch.setenv("MUSIC_ENGINE_MUSIC3_API_KEY_FILE", str(key))
    monkeypatch.setenv("MUSIC_ENGINE_ACE_API_KEY", "wrong-engine-key")
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({"data": {"status": "ok", "engine": ENGINE, "models_initialized": True}}).encode()
    def open_local(request, **options):
        captured.append(request.get_header("Authorization")); return Response()
    monkeypatch.setattr(ace_adapter, "open_local", open_local)
    client = Music3Client()
    client.health(); assert captured == [None]
    key.write_text("music3-key\n"); key.chmod(0o600)
    client.health(); assert captured == [None, "Bearer music3-key"]
    key.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        client.health()


@pytest.mark.parametrize("field", ["reference_audio", "src_audio", "repainting_start", "coverSource"])
def test_music3_transport_refuses_source_conditioning(field):
    client = Music3Client(api_key="test")
    with pytest.raises(ValueError, match="text-to-music only"):
        client.submit({"task_type": "text2music", field: "unsafe-or-unsupported"})


def test_music3_feedback_planner_handles_ending_without_proposing_repaint():
    from local_music_engine.assistant import PlanRequest, rule_plan, plan_from_llm_answer
    request = PlanRequest(feedback="끝이 갑자기 끊겨요", caption="emotional hip hop", lyrics="[Verse]\nStay with me", duration_seconds=30)
    plan = rule_plan(request)
    assert plan["action"] == "regenerate" and plan["range"] is None
    assert not any("파형" in note for note in plan["notes"])
    llm = plan_from_llm_answer(json.dumps({"action": "repaint", "caption": "emotional hip hop, natural ending", "range": {"startSeconds": 20, "endSeconds": 30}, "summary": "끝 구간을 수정해요."}), request, model="writer")
    assert llm["action"] == "regenerate" and llm["range"] is None


def test_music3_diction_plan_preserves_english_vocal_language():
    from local_music_engine.assistant import PlanRequest, rule_plan
    plan = rule_plan(PlanRequest(feedback="발음이 뭉개져요", caption="hip hop", lyrics="[Verse]\nStay with me", duration_seconds=30))
    assert "clear English diction" in plan["stylePrompt"] and "Korean diction" not in plan["stylePrompt"]


def test_music3_overlong_source_regeneration_and_revision_leave_manifest_untouched(tmp_path):
    from local_music_engine.workflow import revise_inputs
    store = ProjectStore.initialize(tmp_path, title="Old long song", lyrics="[Verse]\n가사", style_prompt="pop", target_duration_seconds=301)
    before = store.load()
    with pytest.raises(ValueError, match="300"):
        generate_candidates(tmp_path, seeds=[1], client_factory=FakeMusic3Client)
    assert store.load() == before
    with pytest.raises(ValueError, match="300"):
        revise_inputs(tmp_path, duration_seconds=301)
    assert store.load() == before


def test_timeout_stops_later_submissions_and_explicit_resume_keeps_frozen_seeds(tmp_path):
    make_project(tmp_path)
    class TimedOut(FakeMusic3Client):
        def wait(self, task_id, **options):
            if self.seed == 2:
                raise TimeoutError("remote task may still be running")
            return super().wait(task_id, **options)
    result = generate_candidates(tmp_path, seeds=[1, 2, 3], client_factory=TimedOut)
    before = ProjectStore(tmp_path).load()
    assert FakeMusic3Client.submitted == [1, 2]
    assert result["status"] == "partial" and result["unsubmittedSeeds"] == [3]
    assert result["stopReason"] == "remote-task-timeout"
    assert before["jobs"][0]["parameters"]["seeds"] == [1, 2, 3]
    assert before["jobs"][-1]["remoteTaskId"] == "task-2"
    assert before["jobs"][-1]["status"] == "failed"
    assert len(before["requests"]) == 2
    resumed = resume_latest_batch(tmp_path, client_factory=FakeMusic3Client)
    assert resumed["status"] == "succeeded" and resumed["reusedCandidateIds"] == result["candidateIds"]
    assert FakeMusic3Client.submitted == [1, 2, 2, 3]
    assert ProjectStore(tmp_path).load()["requests"][:2] == before["requests"]


def test_first_seed_timeout_is_failed_and_never_submits_second_seed(tmp_path):
    make_project(tmp_path)
    class TimedOut(FakeMusic3Client):
        def wait(self, task_id, **options): raise TimeoutError("still running")
    result = generate_candidates(tmp_path, seeds=[1, 2], client_factory=TimedOut)
    assert result["status"] == "failed" and result["candidateIds"] == []
    assert result["unsubmittedSeeds"] == [2] and FakeMusic3Client.submitted == [1]


@pytest.mark.parametrize("raw,prepared", [
    ("[Verse] City lights", "[Verse]\nCity lights"),
    ("[Adlib] Yeah", "[Adlib]\nYeah"),
    ("[Verse] [Rap] City lights", "[Verse]\n[Rap]\nCity lights"),
    ("  [Verse - restrained]   City lights  ", "[Verse]\nCity lights  "),
    ("[Verse 2] 너와 함께 걸어\n[Chorus] Stay with me", "[Verse 2]\n너와 함께 걸어\n[Chorus]\nStay with me"),
])
def test_music3_inline_tags_preserve_sung_suffix_under_upstream_normalization(raw, prepared):
    import re
    output, _ = plain_lyrics(raw)
    assert output == prepared
    # Pinned Music 3 normalize_lyrics consumes a tag-led line as a section marker.
    # Compare only sung lines so an inline suffix can never disappear unnoticed.
    def upstream_sung_lines(text):
        return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("[")]
    expected_words = [re.sub(r"^(?:\s*\[[^\]]*\]\s*)+", "", line).strip() for line in raw.splitlines()]
    assert upstream_sung_lines(output) == [line for line in expected_words if line]


def test_music3_inline_lyric_preparation_is_frozen_and_original_manifest_preserved(tmp_path):
    raw = "[Verse] [Rap] City lights\n[Adlib] Yeah\n[Chorus - fuller] Stay with me"
    store = ProjectStore.initialize(tmp_path, title="Inline", lyrics=raw, style_prompt="hip hop", target_duration_seconds=30)
    frozen = _frozen_generation_payload(store.load(), seed=1, model=MODEL, lm_model="unused")
    assert frozen["sourceLyricsOriginal"] == raw
    assert frozen["music3LyricPreparation"]["lyricsOriginal"] == raw
    assert len([change for change in frozen["music3LyricPreparation"]["changes"] if change["kind"] == "inline_tags_separated"]) == 3
    assert "Chorus: fuller" in frozen["prompt"]
    assert store.load()["inputs"]["lyricsOriginal"] == raw
    quality_prepared = prepare_payload(frozen)
    assert quality_prepared["sourceLyricsOriginal"] == raw
    assert quality_prepared["lyrics"].startswith("[Verse]\n[Rap]\nCity lights\n[Adlib]\nYeah")
    assert quality_prepared["music3LyricPreparation"]["changes"] == frozen["music3LyricPreparation"]["changes"]
    assert "Chorus: fuller" in quality_prepared["prompt"]


def test_music3_catalog_reports_character_bound_without_claiming_token_measurement():
    from local_music_engine.production_rules import catalog
    music3 = catalog(); legacy = catalog(engine="ace-step")
    assert music3["captionBudgetCharacters"] == 16000
    assert music3["captionBudgetScope"] == "api_prompt_character_limit"
    assert music3["captionTokenBudgetMeasured"] is False
    assert legacy["captionBudgetCharacters"] == 650
    assert "captionTokenBudgetMeasured" not in legacy
    assert music3["presets"] == legacy["presets"] and music3["rules"] == legacy["rules"]
    legacy["presets"][0]["caption"] = "historical snapshot"
    assert catalog(engine="ace-step")["presets"][0]["caption"] != "historical snapshot"
    parser = build_parser()
    assert run(parser.parse_args(["production-rules"])) == music3
    assert run(parser.parse_args(["production-rules", "--engine", "ace-step"]))["captionBudgetCharacters"] == 650


@pytest.mark.parametrize("duration", [30, 60, 300])
@pytest.mark.parametrize("instrumental", [False, True])
def test_music3_selected_development_maps_actual_advisory_schedule(duration, instrumental):
    raw = "[Instrumental]" if instrumental else "[Verse]\nCity lights are fading\n[Chorus]\nStay until the morning\n[Verse]\nI leave the hurt behind\n[Chorus]\nWe can take it slow"
    project = {"inputs": {"stylePrompt": "warm piano", "lyricsNormalized": raw,
        "targetDurationSeconds": duration, "bpm": 84, "keyScale": "A minor", "timeSignature": "4",
        "productionRules": {"version": 1, "presetId": "emotional-hiphop", "ruleIds": [r["id"] for r in RULES]}}}
    original = deepcopy(project)
    frozen = _frozen_generation_payload(project, seed=1, model=MODEL, lm_model="unused")
    schedule = frozen["music3ArrangementSchedule"]
    assert frozen["music3PromptVersion"] == 3
    assert schedule["advisory"] is True and len(schedule["caption"]) <= 1000
    assert schedule["caption"] in frozen["prompt"].split("[Arrangement]")[1]
    assert schedule["sections"][0]["startSeconds"] == 0
    assert schedule["sections"][-1]["endSeconds"] == duration
    counts = {}
    for expected, entry in zip(frozen["songPlan"]["arrangement"], schedule["sections"], strict=True):
        counts[expected["label"]] = counts.get(expected["label"], 0) + 1
        assert entry == {**expected, "occurrence": counts[expected["label"]]}
    assert schedule["captionMode"] == "musical_section_roles"
    assert " BPM" not in schedule["caption"] and " energy " not in schedule["caption"] and "~" not in schedule["caption"]
    assert project == original and frozen["sourceLyricsOriginal"] == raw
    if instrumental:
        assert frozen["lyrics"] == "[Instrumental]"
        assert "no vocals" in schedule["caption"] and "written verse" not in schedule["caption"]
    else:
        assert "supplied lyric sections in order" in schedule["caption"]
        assert schedule["lyricSections"] == ["Verse", "Chorus", "Verse", "Chorus"]
        assert "Brief intro, restrained verses, gradual build" not in frozen["prompt"]
    if duration == 60 and not instrumental:
        assert any(section["label"] == "Pre-Chorus" for section in schedule["sections"])
        assert "Pre-Chorus" not in schedule["lyricSections"] and "Pre-Chorus" not in schedule["caption"]
        assert schedule["sections"][1]["startSeconds"] == 5.714286


def test_music3_schedule_is_removed_when_development_is_not_selected_even_with_stale_plan():
    from local_music_engine.music3 import translate_payload
    from local_music_engine.song_planning import prepare_song_plan
    raw = "[Verse]\nCity lights\n[Chorus]\nStay with me"
    project = {"inputs": {"stylePrompt": "warm piano", "lyricsNormalized": raw, "targetDurationSeconds": 60,
        "productionRules": {"version": 1, "presetId": "emotional-hiphop", "ruleIds": [r["id"] for r in RULES if r["id"] != "section-development"]}}}
    frozen = _frozen_generation_payload(project, seed=1, model=MODEL, lm_model="unused")
    assert "music3ArrangementSchedule" not in frozen and "Advisory timing" not in frozen["prompt"]
    frozen["songPlan"] = prepare_song_plan(raw, duration_seconds=60, bpm=84, time_signature="4", preset_id="emotional-hiphop", instrumental=False, development=True, breathing=True)
    frozen["music3ArrangementSchedule"] = {"caption": "stale timetable"}
    before = deepcopy(frozen)
    prepared = translate_payload(frozen)
    assert "music3ArrangementSchedule" not in prepared and "Advisory timing" not in prepared["prompt"]
    assert "Brief intro, restrained verses, gradual build" not in prepared["prompt"]
    assert "natural breath between lines" in prepared["prompt"]
    assert frozen == before


def test_music3_old_generic_caption_and_new_schedule_mapping_are_explicitly_distinct():
    from local_music_engine.music3 import translate_payload
    from local_music_engine.song_planning import prepare_song_plan
    raw = "[Verse]\nCity lights\n[Chorus]\nStay with me"
    plan = prepare_song_plan(raw, duration_seconds=60, bpm=84, time_signature="4", preset_id=None,
                             instrumental=False, development=True, breathing=False)
    old = {"engine": ENGINE, "model": MODEL, "seed": 1, "prompt": "[Arrangement]\nBrief intro, restrained verses, gradual build, fuller recurring chorus, gentle ending.",
        "sourceStylePrompt": "piano hip hop", "sourceLyricsOriginal": raw, "lyrics": raw, "audio_duration": 60,
        "music3PromptVersion": 1, "songPlan": plan,
        "productionRules": {"selection": {"ruleIds": ["section-development"]}, "appliedRuleIds": ["section-development"],
                            "appliedCaptions": ["restrained verses, fuller choruses"], "preset": None}}
    before = deepcopy(old)
    new = translate_payload(old)
    assert old == before and old["music3PromptVersion"] == 1
    assert new["music3PromptVersion"] == 3 and "supplied lyric sections in order" in new["prompt"]
    assert "Brief intro, restrained verses, gradual build" not in new["prompt"]
    assert new["lyrics"] == old["lyrics"] and new["sourceLyricsOriginal"] == raw


def test_music3_native_melody_and_performance_controls_are_independent_and_frozen(tmp_path, monkeypatch):
    from local_music_engine import production_rules
    from local_music_engine.music3 import translate_payload
    raw = "[Verse]\nCity lights are fading\n[Chorus]\nStay until the morning"
    selection = {"version": 1, "presetId": "emotional-hiphop",
                 "ruleIds": ["melodic-hook", "expressive-performance"]}
    store = ProjectStore.initialize(tmp_path, title="Hook", lyrics=raw, style_prompt="warm male vocal",
                                   target_duration_seconds=30, production_rules=selection)
    frozen = _frozen_generation_payload(store.load(), seed=1, model=MODEL, lm_model="unused")
    assert frozen["sourceLyricsOriginal"] == frozen["lyrics"] == raw
    assert "rises then settles" in frozen["prompt"] and "intentional dynamics" in frozen["prompt"]
    original = deepcopy(frozen)
    replacement = deepcopy(production_rules.RULES)
    for rule in replacement:
        rule["music3Caption"] = "Unrelated future catalog wording"
    monkeypatch.setattr(production_rules, "RULES", replacement)
    prepared = translate_payload(frozen)
    assert prepared == frozen == original
    assert "Unrelated future" not in prepared["prompt"]
    assert store.load()["inputs"]["lyricsOriginal"] == raw
    # Deselecting a rule must also discard its previously prepared caption.
    changed = deepcopy(frozen)
    changed["productionRules"]["selection"]["ruleIds"] = ["melodic-hook"]
    changed["productionRules"]["appliedRuleIds"] = ["melodic-hook"]
    changed["productionRules"]["appliedCaptions"] = changed["productionRules"]["appliedCaptions"][:2]
    selected = translate_payload(changed)
    assert "intentional dynamics" not in selected["prompt"]
    assert [rule["ruleId"] for rule in selected["music3ProductionGuidance"]["rules"]] == ["melodic-hook"]


def test_old_frozen_music3_rules_keep_their_original_caption_without_catalog_upgrade(tmp_path):
    from local_music_engine.music3 import translate_payload
    selection = {"version": 1, "presetId": None, "ruleIds": ["steady-groove"]}
    store = ProjectStore.initialize(tmp_path, title="Old", lyrics="[Verse]\nStay with me",
                                   style_prompt="hip hop", target_duration_seconds=30,
                                   production_rules=selection)
    old = _frozen_generation_payload(store.load(), seed=1, model=MODEL, lm_model="unused")
    for rule in old["productionRules"]["rules"]:
        for key in list(rule):
            if key.startswith("music3"):
                del rule[key]
    old["music3PromptVersion"] = 2
    before = deepcopy(old)
    new = translate_payload(old)
    assert old == before
    assert "steady tempo, repeating drum groove, kick and bass locked" in new["prompt"]
    assert new["music3ProductionGuidance"]["rules"][0]["native"] is False


def test_music3_instrumental_hook_has_no_sung_or_emotional_vocal_direction(tmp_path):
    selection = {"version": 1, "presetId": "emotional-hiphop",
                 "ruleIds": ["melodic-hook", "expressive-performance"]}
    store = ProjectStore.initialize(tmp_path, title="Theme", lyrics="[Instrumental]",
                                   style_prompt="warm piano", target_duration_seconds=30,
                                   production_rules=selection)
    frozen = _frozen_generation_payload(store.load(), seed=1, model=MODEL, lm_model="unused")
    assert frozen["lyrics"] == "[Instrumental]"
    assert frozen["productionRules"]["skippedRuleIds"] == ["expressive-performance"]
    assert "instrumental motif" in frozen["prompt"]
    assert "sung motif" not in frozen["prompt"] and "emotionally connected lead" not in frozen["prompt"]


def test_music3_six_default_rules_also_receive_frozen_preset_musical_direction(tmp_path, monkeypatch):
    from local_music_engine import production_rules
    from local_music_engine.music3 import translate_payload
    preset = production_rules.PRESETS[0]
    selection = {"version": 1, "presetId": preset["id"], "ruleIds": preset["ruleIds"]}
    store = ProjectStore.initialize(tmp_path, title="Default emotion", lyrics="[Verse]\nCity lights\n[Chorus]\nStay with me",
                                   style_prompt="warm male vocal", target_duration_seconds=30,
                                   production_rules=selection)
    frozen = _frozen_generation_payload(store.load(), seed=1, model=MODEL, lm_model="unused")
    native = frozen["music3ProductionGuidance"]["preset"]
    assert native == {"id": preset["id"], "caption": preset["music3Caption"], "native": True}
    assert "melodic-rap verses" in frozen["prompt"] and "fully sung chorus" in frozen["prompt"]
    assert "quiet hope" in frozen["prompt"] and len(frozen["productionRules"]["appliedRuleIds"]) == 6
    replacement = deepcopy(production_rules.PRESETS)
    replacement[0]["music3Caption"] = "Unrelated future style"
    monkeypatch.setattr(production_rules, "PRESETS", replacement)
    assert translate_payload(frozen) == frozen
    old = deepcopy(frozen)
    for key in list(old["productionRules"]["preset"]):
        if key.startswith("music3"):
            del old["productionRules"]["preset"][key]
    upgraded = translate_payload(old)
    assert upgraded["music3ProductionGuidance"]["preset"]["native"] is False
    assert preset["caption"] in upgraded["prompt"] and "Unrelated future" not in upgraded["prompt"]
