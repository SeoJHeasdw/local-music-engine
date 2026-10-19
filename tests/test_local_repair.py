"""Unsung lines are repaired in the chosen take itself, and only when a re-reading proves it."""

import numpy as np
import pytest

from local_music_engine.auto_quality import quality_policy, repair_target, repair_verdict
from local_music_engine.storage import ProjectStore
from local_music_engine.workflow import generate_candidates, resume_latest_batch
from test_edit_splice import RATE, engine_repaint, music, read, write
from test_workflow import FakeAceClient

VERSE = ["silver morning carries quiet rivers", "under open skies tonight we wander",
         "paper lanterns drifting over harbours", "water slowly turning golden again"]
CHORUS = ["hold me closer when the neon fades away", "and never let the midnight take our song"]
LYRICS = "[Verse]\n" + "\n".join(VERSE) + "\n[Chorus]\n" + "\n".join(CHORUS)


class Engine(FakeAceClient):
    """Twenty-second songs, repainted the way ACE does it."""

    def submit(self, request, source_audio=None):
        self.source_audio, self.range = source_audio, (request.get("repainting_start"), request.get("repainting_end"))
        return super().submit(request, source_audio)

    def download(self, file_path, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.source_audio is None:
            write(destination, music(20, seed=self.seed))
        else:
            write(destination, engine_repaint(read(self.source_audio) / 32767, *self.range, seed=self.seed))


class Listener:
    """Hears the take start singing at 8 s without its first two lines; a repaint may or may not add them."""

    def __init__(self, repaint_sings_them=True):
        self.repaint_sings_them, self.read_again = repaint_sings_them, 0

    @staticmethod
    def reading(*lines):
        return {"available": True, "reliable": True, "language": "en", "loudness": {"status": "unknown"}, "windowAnchorSeconds": 0.0,
                "text": " ".join(text for _, _, text in lines),
                "separation": {"status": "measured", "vocalActivityRegions": [{"startSeconds": 8.0, "endSeconds": 19.0}]},
                "segments": [{"start": start, "end": end, "text": text, "avg_logprob": -0.1, "no_speech_prob": 0.0} for start, end, text in lines]}

    def heard(self, audio):
        rest = [(8.0, 10.0, VERSE[2]), (10.0, 12.0, VERSE[3]), (12.0, 15.5, CHORUS[0]), (15.5, 19.0, CHORUS[1])]
        opening = [(1.0, 4.0, VERSE[0]), (4.0, 7.5, VERSE[1])] if audio.parent.name == "repaints" and self.repaint_sings_them else []
        return self.reading(*opening, *rest)

    def analyze(self, audio, *, progress=None):
        return self.heard(audio)

    def transcribe_span(self, audio, *, start_seconds, end_seconds, language=None):
        self.read_again += 1
        return self.heard(audio)


@pytest.fixture
def song(tmp_path):
    FakeAceClient.submitted = []
    return ProjectStore.initialize(tmp_path / "song", title="보정", lyrics=LYRICS, style_prompt="pop", target_duration_seconds=20)


def make(store, backend, **policy):
    return generate_candidates(store.root, seeds=[1], client_factory=Engine, quality=quality_policy(**{"repair_attempts": 3, **policy}),
                               quality_backend=backend, engine="ace-step")


def audio(store, project, candidate_id):
    candidate = store.find_by_id(project, "candidates", "candidateId", candidate_id)
    return read(store.resolve_artifact(store.find_by_id(project, "artifacts", "artifactId", candidate["artifactId"])))


def test_unsung_lines_are_repainted_into_the_chosen_take_and_the_rest_of_it_is_untouched(song):
    result = make(song, Listener())
    project = song.load()
    assert len(FakeAceClient.submitted) == 2  # one song, one repaint of its opening; no other song was drawn
    winner = song.find_by_id(project, "candidates", "candidateId", result["recommendedCandidateId"])
    repair = winner["quality"]["repair"]
    assert repair["accepted"] and repair["attempt"] == 1 and repair["strength"] == "medium" and repair["rejectedBecause"] == []
    assert (repair["section"], repair["lineCount"], repair["firstLine"]) == ("Verse", 2, VERSE[0])
    assert repair["range"] == {"startSeconds": 0.0, "endSeconds": 7.75}  # the unsung opening, up to just before the singing
    assert winner["quality"]["lyrics"]["status"] == "pass" and winner["quality"]["retryReasons"] == []
    assert winner["quality"]["preferred"] and winner["quality"]["processing"] and winner["humanReview"]["status"] == "unreviewed"
    # The repaint edited the finished take; that take stays in the history as the thing to compare with.
    repainted = song.find_by_id(project, "candidates", "candidateId", winner["parentCandidateId"])
    original = song.find_by_id(project, "candidates", "candidateId", repainted["parentCandidateId"])
    assert repair["ofCandidateId"] == original["candidateId"] and original["quality"]["processing"]
    assert original["quality"]["preferred"] is False and "repair" not in original["quality"]
    assert [item["check"] for item in original["quality"]["lyrics"]["findings"]] == ["lyric_lines_missing"]
    before, after = audio(song, project, original["candidateId"]), audio(song, project, winner["candidateId"])
    changed_until = round(7.9 * RATE)
    assert np.array_equal(after[changed_until:], before[changed_until:]) and not np.array_equal(after[:changed_until], before[:changed_until])
    assert all(song.verify_artifact(artifact)[0] for artifact in project["artifacts"])

    again = resume_latest_batch(song.root, client_factory=Engine, quality_backend=Listener(), engine="ace-step")
    assert again["reusedCandidateIds"] == result["candidateIds"] and again["newCandidateIds"] == []
    assert len(FakeAceClient.submitted) == 2


def test_a_repaint_that_does_not_sing_the_lines_is_kept_only_as_history(song):
    backend = Listener(repaint_sings_them=False)
    result = make(song, backend)
    project = song.load()
    assert len(FakeAceClient.submitted) == 4  # the song and all three repair attempts, then no more
    winner = song.find_by_id(project, "candidates", "candidateId", result["recommendedCandidateId"])
    assert "repair" not in winner["quality"] and winner["parentCandidateId"] == project["candidates"][0]["candidateId"]
    assert winner["editRange"] is None and winner["quality"]["processing"] and winner["quality"]["preferred"]
    tried = [candidate["quality"]["repair"] for candidate in project["candidates"] if candidate["quality"].get("repair")]
    assert [(item["attempt"], item["strength"], item["accepted"]) for item in tried] == [(1, "medium", False), (2, "strong", False), (3, "strong", False)]
    assert all({"no_more_words_heard", "lines_still_unheard"} <= set(item["rejectedBecause"]) for item in tried)
    # The unrepaired song is handed over with the unsung lines still named.
    checks = [song.find_by_id(project, "findings", "findingId", item)["check"] for item in winner["findingIds"]]
    assert "lyric_lines_missing" in checks
    assert len(resume_latest_batch(song.root, client_factory=Engine, quality_backend=backend, engine="ace-step")["newCandidateIds"]) == 0
    assert len(FakeAceClient.submitted) == 4


def test_repair_is_off_unless_asked_for_and_needs_the_lyric_reader(song):
    generate_candidates(song.root, seeds=[1], client_factory=Engine, quality=quality_policy(), quality_backend=Listener(), engine="ace-step")
    assert len(FakeAceClient.submitted) == 1  # the unsung lines are named, nothing is repainted
    assert quality_policy()["repairAttempts"] == 0 and quality_policy("audio", repair_attempts=3)["repairAttempts"] == 0
    with pytest.raises(ValueError):
        quality_policy(repair_attempts=4)


def report(**run):
    unheard = {"section": "Verse", "occurrence": 1, "lineCount": 2, "firstLine": VERSE[0], "syllables": 18, "spanSyllables": 28,
               "searchSpanSeconds": [0.0, 12.0], "rereadConfirmed": True, **run}
    return {"audio": {"retryEligible": False, "findings": []}, "retryReasons": [],
            "lyrics": {"sttReliable": True, "matchedUnits": 20, "unheardLines": [unheard], "findings": []},
            "observation": {"separation": {"vocalActivityRegions": [{"startSeconds": 8.0, "endSeconds": 60.0}]}}}


def test_a_stretch_is_repainted_only_when_the_lines_were_confirmed_unsung_and_there_is_room_to_sing_them():
    assert repair_target(report(), duration_seconds=180)["range"] == {"startSeconds": 0.0, "endSeconds": 7.75}
    assert repair_target(report(rereadConfirmed=False), duration_seconds=180) is None  # one reading is not proof
    # No quiet stretch: the whole span between the sung neighbours, if the words fit in it.
    busy = report(searchSpanSeconds=[40.0, 52.0])
    assert repair_target(busy, duration_seconds=180)["range"] == {"startSeconds": 40.0, "endSeconds": 52.0}
    assert repair_target(report(searchSpanSeconds=[40.0, 44.5], spanSyllables=40), duration_seconds=180) is None  # no room for the words
    assert repair_target(report(searchSpanSeconds=[0.0, 12.0]), duration_seconds=14) is None  # that is most of the song
    silent = report()
    silent["audio"]["retryEligible"] = True
    assert repair_target(silent, duration_seconds=180) is None  # a broken file is regenerated, not patched


def test_a_repair_is_taken_only_when_it_is_verifiably_better_and_nothing_else_got_worse():
    before = report()
    splice = {"applied": True, "changedRange": {"startSeconds": 0.0, "endSeconds": 7.8}}
    fixed = {"audio": {"findings": []}, "retryReasons": [], "lyrics": {"sttReliable": True, "matchedUnits": 31, "findings": []}}
    assert repair_verdict(before, fixed, splice) == []
    assert repair_verdict(before, fixed, {"applied": False, "reason": "source_not_preserved_by_engine"}) == ["rest_of_song_not_preserved"]
    assert repair_verdict(before, fixed, None) == ["rest_of_song_not_preserved"]
    partly = {**fixed, "lyrics": {**fixed["lyrics"], "findings": [{"check": "lyric_lines_missing", "startSeconds": 0.0, "endSeconds": 5.0}]}}
    assert repair_verdict(before, partly, splice) == ["lines_still_unheard"]
    elsewhere = {**fixed, "lyrics": {**fixed["lyrics"], "findings": [{"check": "lyric_lines_missing", "startSeconds": 90.0, "endSeconds": 99.0}]}}
    assert repair_verdict(before, elsewhere, splice) == []  # a different stretch, outside what was repainted
    assert repair_verdict(before, {**fixed, "lyrics": {**fixed["lyrics"], "matchedUnits": 20}}, splice) == ["no_more_words_heard"]
    assert repair_verdict(before, {**fixed, "lyrics": {"sttReliable": False}}, splice) == ["lyrics_not_readable"]
    at = lambda start, check="backing_dropout_suspected": {"check": check, "severity": "warning", "startSeconds": start, "endSeconds": start + 0.25}
    dropout = {**fixed, "rhythm": {"findings": [at(3.0), at(4.0, "loudness_shift")]}}
    assert repair_verdict(before, dropout, splice) == ["new_place_to_check:backing_dropout_suspected"]
    had_it = {**before, "rhythm": {"findings": [at(5.0)]}}
    assert repair_verdict(had_it, dropout, splice) == []  # the stretch already had one
    # The rest of the song is the same samples: what a check says about it is not the repaint's doing.
    elsewhere_dropouts = {**fixed, "rhythm": {"findings": [at(64.0), at(150.25)]}}
    assert repair_verdict(before, elsewhere_dropouts, splice) == []
    whole_song = {**fixed, "rhythm": {"findings": [{"check": "tempo_drift", "severity": "warning", "startSeconds": 0, "endSeconds": 176.0}]}}
    assert repair_verdict(before, whole_song, splice) == ["new_place_to_check:tempo_drift"]
    untimed = {**fixed, "engine": {"findings": [{"check": "engine_meta_mismatch", "severity": "warning", "startSeconds": None, "endSeconds": None}]}}
    assert repair_verdict(before, untimed, splice) == ["new_place_to_check:engine_meta_mismatch"]
    assert repair_verdict(before, {**fixed, "retryReasons": ["duration_mismatch"]}, splice) == ["new_regeneration_reason"]


def clicks(seconds: float, bpm: float, *, start: float = 0.0) -> np.ndarray:
    """A drum-like click on every beat, so the tempo is unambiguous."""
    time = np.arange(round(seconds * RATE)) / RATE
    since = (time - start) % (60 / bpm)
    mono = np.exp(-since * 60) * np.sin(2 * np.pi * 1800 * time) * 0.8
    return np.stack([mono, mono], axis=1)


@pytest.mark.parametrize("played,requested,follow", [(87.4, 84, True), (125.0, 125, False), (84.6, 84, False)])
def test_the_tempo_a_song_actually_plays_is_measured_next_to_the_edit(tmp_path, played, requested, follow):
    from local_music_engine.played_tempo import played_tempo
    song = write(tmp_path / "song.wav", clicks(60, played))
    tempo = played_tempo(song, requested_bpm=requested, start_seconds=20, end_seconds=40)
    assert tempo["status"] == "measured" and abs(tempo["playedBpm"] - played) < 0.25 and tempo["follow"] is follow
    assert 39 <= tempo["measuredSeconds"] <= 40  # both sides, never the stretch being replaced
    silent = write(tmp_path / "silent.wav", np.zeros((30 * RATE, 2)))
    assert played_tempo(silent, requested_bpm=84, start_seconds=10, end_seconds=20)["follow"] is False


def test_a_repaint_of_a_song_that_drifted_is_asked_for_at_the_tempo_it_plays(tmp_path):
    from local_music_engine.workflow import frozen_repaint

    class Clicks(Engine):
        def download(self, file_path, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            write(destination, clicks(60, 87.4) if self.source_audio is None else read(self.source_audio) / 32767)

    FakeAceClient.submitted = []
    store = ProjectStore.initialize(tmp_path / "song", title="템포", lyrics=LYRICS, style_prompt="hip hop",
                                    target_duration_seconds=60, bpm=84, time_signature="4")
    generate_candidates(store.root, seeds=[1], client_factory=Clicks, engine="ace-step")
    project = store.load()
    parent = project["candidates"][0]["candidateId"]
    frozen, *_ = frozen_repaint(store, project, parent_id=parent, start_seconds=20, end_seconds=40, seed=2, strength="strong")
    assert frozen["bpm"] == 87 and frozen["playedTempo"]["requestedBpm"] == 84 and frozen["playedTempo"]["follow"]
    assert store.find_by_id(project, "requests", "requestId", project["candidates"][0]["requestId"])["parameters"]["bpm"] == 84
