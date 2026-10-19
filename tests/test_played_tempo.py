"""Every version records the tempo it actually plays at, bound to its own file."""

import numpy as np

from local_music_engine import auto_quality, cli
from local_music_engine.auto_quality import quality_policy
from local_music_engine.played_tempo import song_tempo
from local_music_engine.storage import ProjectStore
from local_music_engine.workflow import generate_candidates
from test_auto_quality import Reports
from test_edit_splice import RATE, write
from test_local_repair import clicks
from test_rhythm_workflow import structure_report
from test_workflow import FakeAceClient


class Drifted(FakeAceClient):
    """Plays at 87.4 BPM whatever it was asked for, as saved songs asked for 84 did."""

    def download(self, file_path, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        write(destination, clicks(30, 87.4))


def generate(tmp_path, monkeypatch, *, client=Drifted, policy=None, **inputs):
    monkeypatch.setattr(auto_quality, "analyze_music_structure", lambda path, **kwargs: structure_report())
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    store = ProjectStore.initialize(tmp_path / "song", title="템포", lyrics="[Instrumental]", style_prompt="hip hop",
                                    target_duration_seconds=30, time_signature="4", **inputs)
    if policy is None:
        policy = quality_policy("audio", max_attempts=1)
        policy.pop("diagnosticsVersion")
    generate_candidates(store.root, seeds=[1], client_factory=client, quality=policy, quality_backend=Reports(), engine="ace-step")
    return store, store.load()


def test_the_whole_song_is_measured_near_the_tempo_it_was_asked_for(tmp_path):
    song = write(tmp_path / "song.wav", clicks(40, 87.4))
    tempo = song_tempo(song, reference_bpm=84, reference_source="request")
    assert tempo["status"] == "measured" and abs(tempo["playedBpm"] - 87.4) < 0.1
    assert abs(tempo["differencePercent"] - 4.05) < 0.15 and tempo["measuredSeconds"] > 39
    assert tempo["referenceBpm"] == 84 and tempo["searchPercent"] == 12
    silent = write(tmp_path / "silent.wav", np.zeros((30 * RATE, 2)))
    assert song_tempo(silent, reference_bpm=84, reference_source="request")["reason"] == "no_clear_beat"
    assert song_tempo(song, reference_bpm=None, reference_source="none")["reason"] == "no_tempo_to_search_near"
    assert "playedBpm" not in song_tempo(silent, reference_bpm=84, reference_source="request")


def test_each_version_keeps_the_tempo_it_plays_bound_to_its_own_file(tmp_path, monkeypatch):
    store, project = generate(tmp_path, monkeypatch, bpm=84)
    assert len(project["candidates"]) == 2  # the take and its finished copy
    for candidate in project["candidates"]:
        tempo = candidate["quality"]["playedTempo"]
        artifact = store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])
        assert tempo["status"] == "measured" and abs(tempo["playedBpm"] - 87.4) < 0.1
        assert tempo["referenceBpm"] == 84 and tempo["referenceSource"] == "request"
        assert tempo["measuredArtifactSha256"] == artifact["sha256"]
        # A measurement, not a defect: it adds nothing to check and never asks for another take.
        assert not any(store.find_by_id(project, "findings", "findingId", item)["check"].startswith("played")
                       for item in candidate["findingIds"])
        assert candidate["quality"]["retryReasons"] == []
    assert FakeAceClient.submitted == [1]


def test_a_song_left_without_a_tempo_is_measured_near_the_engines_own_choice(tmp_path, monkeypatch):
    class PlannerChose(Drifted):
        def wait(self, task_id, **kwargs):
            return {**super().wait(task_id, **kwargs), "metas": {"bpm": 88, "keyscale": "A minor", "timesignature": "4"}}

    _, project = generate(tmp_path, monkeypatch, client=PlannerChose)
    tempo = project["candidates"][0]["quality"]["playedTempo"]
    assert tempo["referenceSource"] == "engine" and tempo["referenceBpm"] == 88 and abs(tempo["playedBpm"] - 87.4) < 0.1


def test_plans_frozen_before_the_measurement_keep_their_scope(tmp_path, monkeypatch):
    policy = quality_policy("audio", max_attempts=1)
    policy.pop("diagnosticsVersion")
    policy.pop("playedTempoVersion")
    _, project = generate(tmp_path, monkeypatch, policy=policy, bpm=84)
    assert all("playedTempo" not in candidate["quality"] for candidate in project["candidates"])


def test_analyze_rhythm_reports_the_played_tempo(tmp_path):
    song = write(tmp_path / "song.wav", clicks(40, 87.4))
    result = cli.run(cli.build_parser().parse_args(["analyze-rhythm", str(song), "--bpm", "84"]))
    assert abs(result["playedTempo"]["playedBpm"] - 87.4) < 0.1 and result["playedTempo"]["referenceSource"] == "request"
