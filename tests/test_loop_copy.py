"""A BGM version gets its loop as a separate artifact, kept current, exported, and never at the version's expense."""

import shutil
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import butter, sosfiltfilt

from local_music_engine import auto_quality, loop_cut, loops, production_rules
from local_music_engine.auto_quality import quality_policy
from local_music_engine.listening import listening_copies
from local_music_engine.loops import LOOP_KIND, ensure_loop_copy, loop_copies, loop_versions
from local_music_engine.storage import ProjectStore
from local_music_engine.views import candidate_rows, library, project_status
from local_music_engine.workflow import (_frozen_generation_payload, cover_candidates, export_selected, frozen_repaint,
                                         generate_candidates, revise_inputs)
from test_auto_quality import Reports
from test_loop_cut import groove, write
from test_rhythm_workflow import structure_report
from test_workflow import FakeAceClient

CAPTION = production_rules.PURPOSES[0]["caption"]


class Groove(FakeAceClient):
    """Four intro bars, eighteen groove bars and an ending at 120 BPM: one 16-bar loop fits."""

    def download(self, file_path, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        write(destination, groove("iiii" + "g" * 18 + "e", bpm=120))


class NoKick(FakeAceClient):
    def download(self, file_path, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        samples = groove("g" * 20, bpm=120)
        write(destination, sosfiltfilt(butter(4, 300, "high", fs=48000, output="sos"), samples, axis=0))


def _generate(root: Path, monkeypatch, *, client=Groove, purpose="bgm", seeds=(1,)) -> ProjectStore:
    monkeypatch.setattr(auto_quality, "analyze_music_structure", lambda path, **kwargs: structure_report())
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    store = ProjectStore.initialize(root, title="배경", lyrics="[Instrumental]", style_prompt="lofi hip hop, warm piano",
                                    target_duration_seconds=46, bpm=120, time_signature="4", purpose=purpose)
    policy = quality_policy("audio", max_attempts=1)
    policy.pop("diagnosticsVersion")
    generate_candidates(store.root, seeds=list(seeds), client_factory=client, quality=policy, quality_backend=Reports(),
                        engine="ace-step")
    return store


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    monkeypatch = pytest.MonkeyPatch()
    try:
        yield _generate(tmp_path_factory.mktemp("bgm") / "song", monkeypatch)
    finally:
        monkeypatch.undo()


@pytest.fixture
def song(made, tmp_path):
    shutil.copytree(made.root, tmp_path / "song")
    return ProjectStore(tmp_path / "song")


def _finished(project):
    return next(candidate for candidate in project["candidates"] if candidate.get("parentCandidateId"))


def test_a_bgm_version_gets_a_loop_cut_from_its_listening_copy(song):
    project = song.load()
    kinds = sorted(artifact["kind"] for artifact in project["artifacts"])
    assert kinds == ["candidate-audio", "finished-audio", "listening-audio", LOOP_KIND]
    finished = _finished(project)
    listening = listening_copies(project)[finished["artifactId"]]
    loop = loop_copies(project)[listening["artifactId"]]
    assert loop["sourceArtifactSha256"] == listening["sha256"] and loop["candidateId"] == finished["candidateId"]
    assert loop["loop"]["bars"] == 16 and loop["loop"]["startBar"] == 4 and abs(loop["loop"]["bpm"] - 120) < 0.01
    job = song.find_by_id(project, "jobs", "jobId", loop["createdByJobId"])
    batch = next(item for item in project["jobs"] if item["kind"] == "candidate-batch")
    assert job["kind"] == "loop-copy" and job["parentJobId"] == batch["jobId"] and job["outcome"] == {"status": "ready"}
    assert batch["status"] == "succeeded"
    # The loop is a render, not a version: nothing to review, nothing to pick.
    assert {candidate["humanReview"]["status"] for candidate in project["candidates"]} == {"unreviewed"}
    assert len(project["candidates"]) == 2
    rows = {row["candidateId"]: row for row in candidate_rows(song, project)}
    row = rows[finished["candidateId"]]
    assert row["purpose"] == "bgm" and row["loop"]["status"] == "ready" and row["loop"]["bars"] == 16
    assert Path(row["loopPath"]).is_file()
    assert rows[finished["parentCandidateId"]]["loop"] is None  # the replaced raw take
    request = song.find_by_id(project, "requests", "requestId", finished["requestId"])["parameters"]
    assert request["purpose"] == "bgm" and request["prompt"].startswith(CAPTION + ", lofi hip hop")
    assert request["prompt"].count(CAPTION) == 1


def test_cutting_again_adds_nothing(song):
    before = song.load()
    result = loop_versions(song.root)
    after = song.load()
    finished = _finished(after)
    assert result == {"loopArtifactIds": {finished["candidateId"]: loop_copies(before).popitem()[1]["artifactId"]},
                      "noLoop": {}, "failures": {}}
    assert len(after["artifacts"]) == len(before["artifacts"]) and len(after["jobs"]) == len(before["jobs"])


def test_a_changed_cut_retires_the_old_loop_and_removes_its_file(song, monkeypatch):
    old = next(artifact for artifact in song.load()["artifacts"] if artifact["kind"] == LOOP_KIND)
    monkeypatch.setattr(loop_cut, "LOOP_VERSION", "loop-cut-test")
    monkeypatch.setattr(loops, "LOOP_VERSION", "loop-cut-test")
    result = loop_versions(song.root)
    project = song.load()
    new = song.find_by_id(project, "artifacts", "artifactId", next(iter(result["loopArtifactIds"].values())))
    assert new["loop"]["version"] == "loop-cut-test" and new["artifactId"] != old["artifactId"]
    retired = song.find_by_id(project, "artifacts", "artifactId", old["artifactId"])
    assert retired["retired"]["supersededBy"] == new["artifactId"]
    assert not song.resolve_artifact(old).exists() and song.verify_artifact(new)[0]


def test_export_writes_the_loop_or_the_whole_song_but_never_an_unmastered_loop(song, tmp_path):
    finished = _finished(song.load())
    loop = next(artifact for artifact in song.load()["artifacts"] if artifact["kind"] == LOOP_KIND)
    exported = export_selected(song.root, candidate_id=finished["candidateId"], loop=True, output=tmp_path / "out" / "loop.wav")
    assert exported["sha256"] == loop["sha256"] and Path(exported["externalPath"]).is_file()
    assert exported["path"].endswith("-loop.wav")
    whole = export_selected(song.root, candidate_id=finished["candidateId"])
    assert whole["sha256"] == listening_copies(song.load())[finished["artifactId"]]["sha256"]
    with pytest.raises(ValueError, match="no unmastered export"):
        export_selected(song.root, candidate_id=finished["candidateId"], loop=True, unmastered=True)
    with pytest.raises(ValueError, match="no loop to export"):
        export_selected(song.root, candidate_id=finished["parentCandidateId"], loop=True)
    variants = {row["artifactId"]: row["variant"] for row in project_status(song, song.load())["exports"]}
    assert variants == {exported["artifactId"]: "loop", whole["artifactId"]: "song"}


def test_a_kill_while_the_loop_is_staged_is_recovered_and_cut_again(song, monkeypatch):
    finished = _finished(song.load())
    monkeypatch.setattr(loop_cut, "LOOP_VERSION", "loop-cut-test")
    monkeypatch.setattr(loops, "LOOP_VERSION", "loop-cut-test")

    def killed(*args):
        raise SystemExit("killed")

    monkeypatch.setattr(loops.os, "link", killed)
    with pytest.raises(SystemExit):
        ensure_loop_copy(song, finished["candidateId"])
    staged = next(job for job in reversed(song.load()["jobs"]) if job["kind"] == "loop-copy")
    assert staged["status"] == "running" and Path(staged["ownedOutputs"][0]["temporaryPath"]).exists()
    monkeypatch.undo()
    monkeypatch.setattr(loop_cut, "LOOP_VERSION", "loop-cut-test")
    monkeypatch.setattr(loops, "LOOP_VERSION", "loop-cut-test")
    result = loop_versions(song.root)
    project = song.load()
    recovered = song.find_by_id(project, "jobs", "jobId", staged["jobId"])
    assert recovered["status"] == "interrupted" and not Path(staged["ownedOutputs"][0]["temporaryPath"]).exists()
    assert result["loopArtifactIds"] and not result["failures"]


def test_a_song_without_a_kick_records_why_and_is_not_tried_again(tmp_path, monkeypatch):
    store = _generate(tmp_path / "song", monkeypatch, client=NoKick)
    project = store.load()
    assert not any(artifact["kind"] == LOOP_KIND for artifact in project["artifacts"])
    job = next(job for job in project["jobs"] if job["kind"] == "loop-copy")
    assert job["status"] == "succeeded" and job["outcome"]["status"] == "none"
    assert job["outcome"]["code"] in {"no_kick", "tempo_unknown"} and job["outcome"]["reason"]
    row = next(row for row in candidate_rows(store, project) if row["candidateId"] == _finished(project)["candidateId"])
    assert row["loop"] == {"status": "none", "code": job["outcome"]["code"], "reason": job["outcome"]["reason"]}
    result = loop_versions(store.root)
    assert result["noLoop"] == {row["candidateId"]: job["outcome"]["reason"]}
    assert len([item for item in store.load()["jobs"] if item["kind"] == "loop-copy"]) == 1


def test_an_instrumental_that_is_not_bgm_gets_no_loop(tmp_path, monkeypatch):
    store = _generate(tmp_path / "song", monkeypatch, purpose=None)
    project = store.load()
    assert not any(job["kind"] == "loop-copy" for job in project["jobs"])
    assert all(row["loop"] is None for row in candidate_rows(store, project))
    request = project["requests"][0]["parameters"]
    assert "purpose" not in request and "purpose" not in request.get("productionRules", {})
    # A named instrumental version can still be looped by hand.
    assert loop_versions(store.root, candidate_id=_finished(project)["candidateId"])["loopArtifactIds"]


def test_new_versions_edits_and_covers_of_a_bgm_song_stay_bgm(song):
    finished = _finished(song.load())
    generate_candidates(song.root, seeds=[7], client_factory=Groove, engine="ace-step",
                        source_candidate_id=finished["candidateId"])
    cover_candidates(song.root, candidate_id=finished["candidateId"], seeds=[8], client_factory=Groove)
    project = song.load()
    frozen, *_ = frozen_repaint(song, project, parent_id=finished["candidateId"], start_seconds=10, end_seconds=14, seed=9)
    requests = [request["parameters"] for request in project["requests"]][-2:] + [frozen]
    for parameters in requests:
        assert parameters["purpose"] == "bgm" and parameters["productionRules"]["purpose"]["caption"] == CAPTION
        assert parameters["prompt"].count(CAPTION) == 1


def test_bgm_refuses_lyrics_and_music3(tmp_path, song):
    with pytest.raises(ValueError, match="BGM has no lyrics"):
        ProjectStore.initialize(tmp_path / "sung", title="x", lyrics="[Verse]\n안녕", style_prompt="pop",
                                target_duration_seconds=60, purpose="bgm")
    before = song.manifest_path.read_bytes()
    with pytest.raises(ValueError, match="BGM has no lyrics"):
        revise_inputs(song.root, lyrics="[Verse]\n안녕")
    assert song.manifest_path.read_bytes() == before
    with pytest.raises(ValueError, match="ACE engine only"):
        _frozen_generation_payload(song.load(), seed=1, model="music-3", lm_model="", engine="minimax-music3")
    with pytest.raises(ValueError, match="unknown song purpose"):
        ProjectStore.initialize(tmp_path / "other", title="x", lyrics="[Instrumental]", style_prompt="pop",
                                target_duration_seconds=60, purpose="podcast")


def test_the_catalog_and_library_name_the_purpose(song):
    assert [purpose["id"] for purpose in production_rules.catalog(engine="ace-step")["purposes"]] == ["bgm"]
    assert "purposes" not in production_rules.catalog(engine="minimax-music3")
    summary = library(song.root.parent)[0]
    assert summary["purpose"] == "bgm" and summary["instrumental"] is True
    assert project_status(song, song.load())["inputs"]["purpose"] == "bgm"
    np.testing.assert_equal(len(CAPTION) < 200, True)
