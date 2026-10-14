"""An edit changes its own stretch; every other sample stays the source's."""

import wave
from pathlib import Path

import numpy as np
import pytest

from local_music_engine.auto_quality import quality_policy
from local_music_engine.edit_splice import restore_outside_edit
from local_music_engine.storage import ProjectStore
from local_music_engine.views import project_status
from local_music_engine.workflow import generate_candidates, repaint_candidate
from test_auto_quality import LYRICS, Reports
from test_workflow import FakeAceClient

RATE = 48_000


def music(seconds: float, *, seed: int, peak: float = 0.8913, channels: int = 2) -> np.ndarray:
    """Tones with drum-like bursts, scaled so the loudest sample sits at ``peak``."""
    random = np.random.default_rng(seed)
    time = np.arange(round(seconds * RATE)) / RATE
    tone = sum(np.sin(2 * np.pi * frequency * time + phase) for frequency, phase in zip((110, 220, 331, 495), random.uniform(0, 6, 4)))
    burst = random.normal(size=len(time)) * np.exp(-((time * 2) % 1) * 30)
    mono = tone / 4 + burst
    return np.stack([mono * (1 - 0.1 * channel) for channel in range(channels)], axis=1) / np.abs(mono).max() * peak


def write(path: Path, samples: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(samples.shape[1])
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(np.rint(samples * 32767).astype("<i2").tobytes())
    return path


def read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as audio:
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").reshape(-1, audio.getnchannels())


def engine_repaint(source: np.ndarray, start: float, end: float, *, seed: int, louder: float = 1.12) -> np.ndarray:
    """What ACE returns: new audio in the range, the source around it, the whole file peak-normalized."""
    fresh = music(len(source) / RATE, seed=seed, channels=source.shape[1])
    fresh *= 0.8913 * louder / np.abs(fresh[round(start * RATE):round(end * RATE)]).max()
    time = np.arange(len(source)) / RATE
    mask = np.clip(np.minimum(time - (start - 0.025), (end + 0.025) - time) / 0.025, 0, 1)[:, None]
    mixed = mask * fresh + (1 - mask) * source
    return mixed / np.abs(mixed).max() * 0.8913


def test_only_the_edited_stretch_differs_and_the_song_is_not_turned_down(tmp_path):
    source = music(20, seed=1)
    parent = write(tmp_path / "parent.wav", source)
    edited = write(tmp_path / "edited.wav", engine_repaint(source, 6, 10, seed=2))
    engine = read(edited).astype(float)
    assert 0.5 < np.abs(engine[: 2 * RATE]).max() / np.abs(read(parent)[: 2 * RATE]).max() < 0.95  # the engine lowered the rest

    record = restore_outside_edit(parent, edited, start_seconds=6, end_seconds=10)

    assert record["applied"] and -1.2 < record["gainDb"] < -0.8 and record["residualDb"] < -60
    changed = record["changedRange"]
    assert 5.9 < changed["startSeconds"] <= 6 and 10 <= changed["endSeconds"] < 10.1
    result, original = read(edited), read(parent)
    first, last = round(changed["startSeconds"] * RATE), round(changed["endSeconds"] * RATE)
    assert np.array_equal(result[:first], original[:first]) and np.array_equal(result[last:], original[last:])
    assert not np.array_equal(result[first:last], original[first:last])
    # The new stretch keeps the engine's balance against the rest: only its peaks were lowered.
    restored = engine[first:last] / record["gain"]
    assert np.abs(result).max() <= np.abs(original).max()
    assert 0 < record["peakReductionDb"] <= 1.2 and record["peakReducedSeconds"] < 0.5
    loudness = lambda samples: 10 * np.log10((samples.astype(float) ** 2).mean())
    assert abs(loudness(result[first:last]) - loudness(restored)) < 0.1
    # No step at either join beyond what the source itself does there.
    for index in (first, last):
        assert abs(int(result[index, 0]) - int(result[index - 1, 0])) <= abs(int(original[index, 0]) - int(original[index - 1, 0])) + 8


def test_a_file_the_engine_did_not_build_from_the_source_is_left_as_made(tmp_path):
    parent = write(tmp_path / "parent.wav", music(20, seed=1))
    edited = write(tmp_path / "edited.wav", music(20, seed=3))
    before = edited.read_bytes()
    record = restore_outside_edit(parent, edited, start_seconds=6, end_seconds=10)
    assert record["applied"] is False and record["reason"] == "source_not_preserved_by_engine"
    assert edited.read_bytes() == before


@pytest.mark.parametrize("start,end,reason", [(0.5, 19.5, "no_stretch_far_from_the_edit")])
def test_an_edit_with_no_untouched_stretch_cannot_be_matched(tmp_path, start, end, reason):
    source = music(20, seed=1)
    parent = write(tmp_path / "parent.wav", source)
    edited = write(tmp_path / "edited.wav", engine_repaint(source, start, end, seed=2))
    before = edited.read_bytes()
    assert restore_outside_edit(parent, edited, start_seconds=start, end_seconds=end)["reason"] == reason
    assert edited.read_bytes() == before


def test_a_far_louder_edit_or_another_format_keeps_the_engine_file(tmp_path):
    source = music(20, seed=1)
    parent = write(tmp_path / "parent.wav", source)
    edited = write(tmp_path / "edited.wav", engine_repaint(source, 6, 10, seed=2, louder=2.0))
    assert restore_outside_edit(parent, edited, start_seconds=6, end_seconds=10)["reason"] == "edit_much_louder_than_the_source"
    mono = write(tmp_path / "mono.wav", music(20, seed=1, channels=1))
    assert restore_outside_edit(parent, mono, start_seconds=6, end_seconds=10)["reason"] == "format_differs_from_source"
    short = write(tmp_path / "short.wav", source[: 10 * RATE])
    assert restore_outside_edit(parent, short, start_seconds=6, end_seconds=9)["reason"] == "length_differs_from_source"


class EngineLikeClient(FakeAceClient):
    """Generates one song, and repaints it the way ACE does."""

    def submit(self, request, source_audio=None):
        self.source_audio, self.range = source_audio, (request.get("repainting_start"), request.get("repainting_end"))
        return super().submit(request, source_audio)

    def download(self, file_path, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.source_audio is None:
            write(destination, music(10, seed=self.seed))
        else:
            write(destination, engine_repaint(read(self.source_audio) / 32767, *self.range, seed=self.seed))


def test_a_repaint_and_its_finished_copy_keep_the_parent_outside_the_edit(tmp_path):
    FakeAceClient.submitted = []
    store = ProjectStore.initialize(tmp_path / "song", title="편집", lyrics=LYRICS,
                                    style_prompt="Korean acoustic pop", target_duration_seconds=10)
    generate_candidates(store.root, seeds=[1], client_factory=EngineLikeClient, quality=quality_policy(),
                        quality_backend=Reports(), engine="ace-step")
    parent_id = store.load()["recommendedCandidateId"]  # the finished copy, which is what a listener edits
    result = repaint_candidate(store.root, candidate_id=parent_id, start_seconds=4, end_seconds=6, seed=8,
        client_factory=EngineLikeClient, quality=quality_policy(max_attempts=1), quality_backend=Reports(), engine="ace-step")
    project = store.load()
    audio = lambda candidate_id: read(store.resolve_artifact(store.find_by_id(
        project, "artifacts", "artifactId", store.find_by_id(project, "candidates", "candidateId", candidate_id)["artifactId"])))
    raw = store.find_by_id(project, "candidates", "candidateId", project["candidates"][-2]["candidateId"])
    splice = store.find_by_id(project, "artifacts", "artifactId", raw["artifactId"])["splice"]
    assert splice["applied"] and len(splice["engineOutputSha256"]) == 64
    first, last = round(splice["changedRange"]["startSeconds"] * RATE), round(splice["changedRange"]["endSeconds"] * RATE)
    parent, finished = audio(parent_id), audio(result["candidateId"])
    assert result["candidateId"] != raw["candidateId"]
    assert np.array_equal(finished[:first], parent[:first]) and np.array_equal(finished[last:], parent[last:])
    assert not np.array_equal(finished[first:last], parent[first:last])
    row = next(item for item in project_status(store, project)["candidates"] if item["candidateId"] == result["candidateId"])
    assert row["splice"]["applied"] and row["splice"]["changedRange"] == splice["changedRange"]
