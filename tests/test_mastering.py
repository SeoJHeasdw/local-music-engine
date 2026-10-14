"""The mastered listening copy: its level and timing, and that the listener gets it while edits do not."""

import wave
from pathlib import Path

import numpy as np
import pytest

from local_music_engine import mastering
from local_music_engine.listening import _inherited_drive, listening_copies, master_versions
from local_music_engine.mastering import LIMIT, MasteringError, master_audio
from local_music_engine.qc import inspect_wav
from local_music_engine.views import candidate_rows
from local_music_engine.workflow import export_selected
from test_auto_quality import generate, song  # noqa: F401 - the fixture is used by name


def _write(path: Path, seconds: float = 8.0, *, rate: int = 48000, level: float = 0.2) -> None:
    """A kick every half second, a held chord and hi-hats: enough music for loudness gating."""

    t = np.arange(int(seconds * rate)) / rate
    kick = np.sin(2 * np.pi * 55 * t) * np.exp(-(t % 0.5) * 12)
    chord = sum(np.sin(2 * np.pi * frequency * t) for frequency in (220.0, 277.2, 329.6)) / 3
    hats = np.random.default_rng(7).standard_normal(len(t)) * np.exp(-(t % 0.25) * 60)
    mono = level * (0.6 * kick + 0.3 * chord + 0.1 * hats)
    stereo = np.stack([mono, 0.9 * np.roll(mono, 24)], 1)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((np.clip(stereo, -1, 1) * 32767).astype("<i2").tobytes())


def _read(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path)) as audio:
        width, channels, rate = audio.getsampwidth(), audio.getnchannels(), audio.getframerate()
        raw = np.frombuffer(audio.readframes(audio.getnframes()), np.uint8)
    if width == 3:
        triples = raw.reshape(-1, 3).astype(np.int32)
        values = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        samples = np.where(values >= 1 << 23, values - (1 << 24), values) / 2 ** 23
    else:
        samples = raw.view("<i2") / 32768
    return rate, samples.reshape(-1, channels)


def test_master_reaches_release_loudness_without_moving_or_resizing_the_song(tmp_path: Path) -> None:
    source, copy = tmp_path / "version.wav", tmp_path / "listening.wav"
    _write(source)
    report = master_audio(source, copy, channels=2, duration_seconds=8.0)
    assert abs(report["output"]["integratedLufs"] - mastering.TARGET_LUFS) <= 0.5
    assert report["driveInherited"] is False
    rate_in, before = _read(source)
    rate_out, after = _read(copy)
    assert rate_out == rate_in and after.shape == before.shape
    assert np.abs(after).max() <= LIMIT + 1e-3
    # The limiter's 5 ms look-ahead (240 samples) is compensated, so playhead times and edit
    # ranges stay valid. What remains is the 25 Hz high-pass's phase on low notes, a few samples.
    window = slice(2 * rate_in, 4 * rate_in)
    reference, mastered = before[window].mean(1), after[window].mean(1)
    lags = range(-400, 401)
    best = max(lags, key=lambda lag: float(np.dot(np.roll(mastered, -lag), reference)))
    assert abs(best) <= 24
    assert inspect_wav(copy)["channels"] == 2


def test_an_inherited_drive_reproduces_the_same_copy(tmp_path: Path) -> None:
    source = tmp_path / "version.wav"
    _write(source)
    first = master_audio(source, tmp_path / "a.wav", channels=2, duration_seconds=8.0, drive_db=4.0)
    master_audio(source, tmp_path / "b.wav", channels=2, duration_seconds=8.0, drive_db=4.0)
    assert first["driveInherited"] is True and first["driveDb"] == 4.0
    assert (tmp_path / "a.wav").read_bytes() == (tmp_path / "b.wav").read_bytes()


@pytest.mark.parametrize(("seconds", "level"), [(2.0, 0.2), (5.0, 0.0)])
def test_short_or_silent_audio_is_not_mastered(tmp_path: Path, seconds: float, level: float) -> None:
    source, copy = tmp_path / "version.wav", tmp_path / "listening.wav"
    _write(source, seconds, level=level)
    with pytest.raises(MasteringError):
        master_audio(source, copy, channels=2, duration_seconds=seconds)
    assert not copy.exists() and list(tmp_path.iterdir()) == [source]


def test_the_listener_hears_and_exports_the_copy_while_the_version_stays_as_made(song, tmp_path: Path) -> None:
    generate(song)
    project = song.load()
    raw, finished = project["candidates"]
    copy = listening_copies(project)[finished["artifactId"]]
    job = song.find_by_id(project, "jobs", "jobId", copy["createdByJobId"])
    assert job["kind"] == "listening-copy" and job["status"] == "succeeded" and job["parentJobId"]
    assert copy["sourceArtifactSha256"] == song.find_by_id(project, "artifacts", "artifactId", finished["artifactId"])["sha256"]
    rows = {row["candidateId"]: row for row in candidate_rows(song, project)}
    assert rows[finished["candidateId"]]["listeningPath"] == str(song.resolve_artifact(copy))
    assert rows[raw["candidateId"]]["listeningPath"] == ""
    assert export_selected(song.root, candidate_id=finished["candidateId"])["sha256"] == copy["sha256"]
    unmastered = export_selected(song.root, candidate_id=finished["candidateId"], unmastered=True)
    assert unmastered["sha256"] == song.find_by_id(project, "artifacts", "artifactId", finished["artifactId"])["sha256"]
    before = len(song.load()["artifacts"])
    assert master_versions(song.root) == {"listeningArtifactIds": {finished["candidateId"]: copy["artifactId"]}, "failures": {}}
    assert len(song.load()["artifacts"]) == before


def test_an_edit_keeps_the_level_of_the_mastered_version_it_edits() -> None:
    edit = {"startSeconds": 10.0, "endSeconds": 20.0}
    project = {"candidates": [
        {"candidateId": "version", "artifactId": "a-version", "parentCandidateId": None, "editRange": None},
        {"candidateId": "edit-raw", "artifactId": "a-edit-raw", "parentCandidateId": "version", "editRange": edit},
        {"candidateId": "edit", "artifactId": "a-edit", "parentCandidateId": "edit-raw", "editRange": edit},
    ]}
    copies = {"a-version": {"mastering": {"driveDb": 7.5}}}
    assert _inherited_drive(project, project["candidates"][2], copies) == 7.5
    assert _inherited_drive(project, project["candidates"][0], copies) is None
