import copy
import json
from pathlib import Path
from typing import Any

import pytest

from local_music_engine import cli, drafting, production_rules
from local_music_engine.assistant import LlmConfig
from local_music_engine.storage import ProjectStore
from local_music_engine.views import project_status
from local_music_engine.workflow import generate_candidates, repaint_candidate, resume_latest_batch, revise_inputs
from test_workflow import RecordingAceClient


def selection(*rule_ids: str, preset: str | None = "emotional-hiphop") -> dict[str, Any]:
    return {"version": 1, "presetId": preset, "ruleIds": list(rule_ids)}


@pytest.fixture(autouse=True)
def reset_recording_client():
    RecordingAceClient.payloads = []
    RecordingAceClient.submitted = []
    RecordingAceClient.fail_seeds = set()
    RecordingAceClient.loaded = ("acestep-v15-turbo", "acestep-5Hz-lm-4B")
    yield
    RecordingAceClient.fail_seeds = set()


def initialize(root: Path, **changes: Any) -> ProjectStore:
    values = dict(title="rules", lyrics="[Verse]\nStay with me\n[Chorus]\nThrough the night",
                  style_prompt="soft male vocal", target_duration_seconds=30,
                  production_rules=selection("steady-groove", "clear-vocal", "simple-structure"))
    values.update(changes)
    return ProjectStore.initialize(root, **values)


def test_rules_reach_actual_engine_payload_and_preserve_editable_base(tmp_path: Path) -> None:
    store = initialize(tmp_path, style_prompt="")
    result = generate_candidates(tmp_path, seeds=[1], client_factory=RecordingAceClient, engine="ace-step")
    project = store.load()
    frozen = project["requests"][0]["parameters"]
    actual = RecordingAceClient.payloads[0]
    assert actual["prompt"] == frozen["prompt"]
    assert actual["prompt"].startswith("emotional hip hop")
    assert "steady tempo" in actual["prompt"] and "single clear lead vocal" in actual["prompt"]
    assert "one short verse" in actual["prompt"]
    assert (actual["bpm"], actual["key_scale"], actual["time_signature"]) == (84, "A minor", "4")
    assert actual["vocal_language"] == "en"
    assert "productionRules" not in actual
    assert project["inputs"]["stylePrompt"] == ""
    assert "bpm" not in project["inputs"]
    row = project_status(store, project)["candidates"][0]
    assert row["baseStylePrompt"] == ""
    assert row["productionRules"]["selection"] == project["inputs"]["productionRules"]
    assert row["humanReview"]["status"] == "unreviewed"
    assert result["status"] == "succeeded"


def test_explicit_metadata_wins_and_old_stored_meter_is_only_normalized_for_new_request(tmp_path: Path) -> None:
    store = initialize(tmp_path, bpm=96, key_scale="D minor", time_signature="6/8")
    assert store.load()["inputs"]["timeSignature"] == "6"
    with store.transaction() as project:
        project["inputs"]["timeSignature"] = "3/4"
    generate_candidates(tmp_path, seeds=[1], client_factory=RecordingAceClient, engine="ace-step")
    actual = RecordingAceClient.payloads[0]
    assert (actual["bpm"], actual["key_scale"], actual["time_signature"]) == (96, "D minor", "3")
    assert store.load()["inputs"]["timeSignature"] == "3/4"


def test_instrumental_skips_vocal_rules_and_uses_duration_specific_structure(tmp_path: Path) -> None:
    store = initialize(tmp_path, lyrics="[Instrumental]")
    generate_candidates(tmp_path, seeds=[1], client_factory=RecordingAceClient, engine="ace-step")
    assert "lead vocal" not in RecordingAceClient.payloads[0]["prompt"]
    assert "one recurring main theme" in RecordingAceClient.payloads[0]["prompt"]
    assert store.load()["requests"][0]["parameters"]["productionRules"]["skippedRuleIds"] == ["clear-vocal"]
    revise_inputs(tmp_path, duration_seconds=60)
    generate_candidates(tmp_path, seeds=[2], client_factory=RecordingAceClient, engine="ace-step")
    assert "smooth transitions" in RecordingAceClient.payloads[1]["prompt"]
    assert "one recurring main theme" not in RecordingAceClient.payloads[1]["prompt"]


def test_source_and_resume_replay_frozen_guidance_after_catalog_and_inputs_change(tmp_path: Path, monkeypatch) -> None:
    store = initialize(tmp_path)
    RecordingAceClient.fail_seeds = {2}
    first = generate_candidates(tmp_path, seeds=[1, 2], client_factory=RecordingAceClient, engine="ace-step")
    original_prompt = RecordingAceClient.payloads[0]["prompt"]
    original_snapshot = store.load()["requests"][0]["parameters"]["productionRules"]
    changed = copy.deepcopy(production_rules.PRESETS)
    changed[0]["caption"] = "catalog changed genre"
    changed[0]["bpm"] = 100
    monkeypatch.setattr(production_rules, "PRESETS", changed)
    changed_rules = copy.deepcopy(production_rules.RULES)
    changed_rules[0]["caption"] = "catalog changed groove"
    monkeypatch.setattr(production_rules, "RULES", changed_rules)
    revise_inputs(tmp_path, style_prompt="completely different", production_rules=selection(preset="upbeat-pop"), bpm=140)
    RecordingAceClient.fail_seeds = set()
    resumed = resume_latest_batch(tmp_path, client_factory=RecordingAceClient, engine="ace-step")
    assert resumed["status"] == "succeeded"
    assert RecordingAceClient.payloads[-1]["prompt"] == original_prompt
    assert RecordingAceClient.payloads[-1]["bpm"] == 84
    generate_candidates(tmp_path, seeds=[3], source_candidate_id=first["candidateIds"][0], client_factory=RecordingAceClient, engine="ace-step")
    assert RecordingAceClient.payloads[-1]["prompt"] == original_prompt
    assert RecordingAceClient.payloads[-1]["bpm"] == 84
    assert store.load()["requests"][-1]["parameters"]["productionRules"] == original_snapshot
    assert store.load()["inputs"]["stylePrompt"] == "soft male vocal"
    assert store.load()["inputs"]["bpm"] is None


def test_repaint_effective_prompt_override_does_not_duplicate_rules(tmp_path: Path) -> None:
    store = initialize(tmp_path)
    candidate = generate_candidates(tmp_path, seeds=[1], client_factory=RecordingAceClient, engine="ace-step")["candidateIds"][0]
    original = store.load()["requests"][0]["parameters"]
    repaint_candidate(tmp_path, candidate_id=candidate, start_seconds=1, end_seconds=2, seed=2,
                      style_prompt=original["prompt"], client_factory=RecordingAceClient, engine="ace-step")
    actual = RecordingAceClient.payloads[-1]
    assert actual["prompt"] == original["prompt"]
    assert actual["prompt"].count("steady tempo") == 1
    assert store.load()["requests"][-1]["parameters"]["productionRules"]["baseStylePrompt"] == "soft male vocal"


def test_lyrics_only_repaint_keeps_the_parent_style_and_sends_the_new_words(tmp_path: Path) -> None:
    # The app's 가사 tab sends the version's base style unchanged with rewritten lyrics.
    store = initialize(tmp_path)
    candidate = generate_candidates(tmp_path, seeds=[1], client_factory=RecordingAceClient, engine="ace-step")["candidateIds"][0]
    original = store.load()["requests"][0]["parameters"]
    edited = "[Verse]\nStay with me tonight\n[Chorus]\nThrough the night"
    repaint_candidate(tmp_path, candidate_id=candidate, start_seconds=1, end_seconds=2, seed=2,
                      style_prompt="soft male vocal", lyrics=edited, strength="medium",
                      client_factory=RecordingAceClient, engine="ace-step")
    actual = RecordingAceClient.payloads[-1]
    assert actual["prompt"] == original["prompt"]
    assert "Stay with me tonight" in actual["lyrics"]
    request = store.load()["requests"][-1]["parameters"]
    assert request["productionRules"]["baseStylePrompt"] == "soft male vocal"
    assert store.load()["candidates"][0]["candidateId"] == candidate


@pytest.mark.parametrize("invalid", [None, {}, {"version": True, "presetId": None, "ruleIds": []},
    selection("unknown"), selection(preset="unknown"), selection(3),
    {"version": 1, "presetId": None, "ruleIds": "steady-groove"},
    dict(selection(), extra=True)])
def test_strict_selection_validation(invalid: Any) -> None:
    with pytest.raises(ValueError):
        production_rules.normalize_selection(invalid)


def test_invalid_selection_and_useless_instrumental_guidance_do_not_mutate_project(tmp_path: Path) -> None:
    root = tmp_path / "new"
    with pytest.raises(ValueError):
        initialize(root, production_rules=selection("unknown"))
    assert not root.exists()
    with pytest.raises(ValueError, match="style"):
        initialize(root, style_prompt="", lyrics="[Instrumental]", production_rules=selection("clear-vocal", preset=None))
    assert not root.exists()
    store = initialize(root)
    before = store.manifest_path.read_bytes()
    with pytest.raises(ValueError):
        revise_inputs(root, title="must not persist", production_rules=selection("unknown"))
    assert store.manifest_path.read_bytes() == before
    with pytest.raises(ValueError):
        cli.run(cli.build_parser().parse_args(["init", str(tmp_path / "bad-meter"), "--title", "x", "--lyrics", "x", "--style", "x", "--duration", "30", "--time-signature", "5/4"]))
    assert not (tmp_path / "bad-meter").exists()


def test_cli_propagates_draft_controls_and_returns_catalog(monkeypatch) -> None:
    captured = {}
    def fake_draft(description: str, **kwargs: Any):
        captured.update(description=description, **kwargs)
        return {"title": "draft"}
    monkeypatch.setattr(cli, "draft_song", fake_draft)
    chosen = selection("steady-groove", "simple-structure")
    args = cli.build_parser().parse_args(["draft", "--query", "감성 힙합", "--duration", "30", "--vocal-language", "en", "--production-rules-json", json.dumps(chosen)])
    assert cli.run(args) == {"title": "draft"}
    assert captured["production_rules"] == chosen and captured["vocal_language"] == "en"
    assert cli.run(cli.build_parser().parse_args(["production-rules"]))["version"] == 1


def test_presets_keep_six_defaults_and_new_planning_controls_remain_selectable() -> None:
    catalog = production_rules.catalog()
    defaults = ["steady-groove", "repeated-harmony", "focused-arrangement", "clear-vocal", "clean-production", "simple-structure"]
    assert all(preset["ruleIds"] == defaults for preset in catalog["presets"])
    assert {"section-development", "phrase-breathing"} <= {rule["id"] for rule in catalog["rules"]}
    chosen = selection(*defaults, "section-development", "phrase-breathing")
    assert production_rules.normalize_selection(chosen) == chosen


def test_selected_rules_are_in_actual_engine_and_llm_draft_requests(monkeypatch) -> None:
    requests = []
    class SampleClient:
        def __init__(self, base_url: str):
            pass
        def create_sample(self, query: str, **kwargs: Any):
            requests.append({"query": query, **kwargs})
            return {"caption": "emotional hip hop", "lyrics": "[Verse]\nStay with me\n[Chorus]\nThrough the night", "duration": 150}
    chosen = selection("steady-groove", "simple-structure")
    result = drafting.draft_song("감성 힙합", duration_seconds=30, instrumental=False, vocal_language="en",
                                 production_rules=chosen, base_url="http://127.0.0.1:1", client_factory=SampleClient, engine="ace-step")
    assert "steady tempo" in requests[0]["query"]
    assert "one short verse" in requests[0]["query"] and requests[0]["vocal_language"] == "en"
    assert result["lyrics"].startswith("[Verse]\nStay") and result["durationSeconds"] == 30
    assert (result["bpm"], result["keyScale"], result["timeSignature"]) == (84, "A minor", "4")
    def chat(config, messages, **kwargs):
        requests.append(messages)
        return json.dumps({"caption": "emotional hip hop", "lyrics": "[Verse]\nStay with me\n[Chorus]\nThrough the night"})
    monkeypatch.setattr(drafting, "_chat", chat)
    drafting.draft_song("감성 힙합", duration_seconds=30, instrumental=False, vocal_language="en", production_rules=chosen,
                        base_url="http://127.0.0.1:1", llm=LlmConfig(base_url="http://127.0.0.1:11434", model="test"), engine="ace-step")
    llm_request = json.loads(requests[-1][1]["content"])
    assert "steady tempo" in " ".join(llm_request["productionGuidance"]["captions"])
    assert llm_request["requestedVocalLanguage"] == "en" and llm_request["requestedLengthSeconds"] == 30
    assert "only a short [Verse]" in requests[-1][0]["content"]


def test_empty_draft_selection_preserves_omitted_controls(monkeypatch) -> None:
    requests = []
    class SampleClient:
        def __init__(self, base_url: str):
            pass
        def create_sample(self, query: str, **kwargs: Any):
            requests.append({"query": query, **kwargs})
            return {"caption": "emotional hip hop", "lyrics": "[Verse]\nStay with me", "duration": 150}
    common = dict(duration_seconds=30, instrumental=False, vocal_language="en",
                  base_url="http://127.0.0.1:1", client_factory=SampleClient)
    omitted = drafting.draft_song("감성 힙합", **common, engine="ace-step")
    explicit_empty = drafting.draft_song("감성 힙합", production_rules=selection(preset=None), **common, engine="ace-step")
    assert omitted == explicit_empty
    assert requests[0] == requests[1]
    assert explicit_empty["durationSeconds"] == 150 and explicit_empty["notes"] == []
    def chat(config, messages, **kwargs):
        requests.append(messages)
        return json.dumps({"caption": "emotional hip hop", "lyrics": "[Verse]\nStay with me", "durationSeconds": 150})
    monkeypatch.setattr(drafting, "_chat", chat)
    common["llm"] = LlmConfig(base_url="http://127.0.0.1:11434", model="test")
    omitted = drafting.draft_song("감성 힙합", **common, engine="ace-step")
    explicit_empty = drafting.draft_song("감성 힙합", production_rules=selection(preset=None), **common, engine="ace-step")
    assert omitted == explicit_empty and requests[-2] == requests[-1]
    request = json.loads(requests[-1][1]["content"])
    assert "productionGuidance" not in request and "captionInstruction" not in request


def test_feedback_planner_uses_empty_base_without_readding_effective_prompt(tmp_path: Path) -> None:
    initialize(tmp_path, style_prompt="")
    candidate = generate_candidates(tmp_path, seeds=[1], client_factory=RecordingAceClient, engine="ace-step")["candidateIds"][0]
    args = cli.build_parser().parse_args(["plan", str(tmp_path), candidate, "--feedback", "드럼이 너무 세요"])
    plan = cli.run(args)
    assert plan["baseStylePrompt"] == ""
    assert "steady tempo" not in plan["stylePrompt"]
    unknown = cli.run(cli.build_parser().parse_args(["plan", str(tmp_path), candidate, "--feedback", "잘 모르겠어요"]))
    assert unknown["stylePrompt"].strip()
    generate_candidates(tmp_path, seeds=[2], source_candidate_id=candidate, style_prompt=unknown["stylePrompt"], client_factory=RecordingAceClient, engine="ace-step")
    repaint_candidate(tmp_path, candidate_id=candidate, start_seconds=1, end_seconds=2, seed=3,
                      style_prompt=unknown["stylePrompt"], client_factory=RecordingAceClient, engine="ace-step")
    assert RecordingAceClient.payloads[-1]["prompt"].count("emotional hip hop") == 1
