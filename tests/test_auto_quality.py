"""The production automatic workflow: bounded attempts, immutable history and recovery."""

from copy import deepcopy
from pathlib import Path
import signal
import subprocess
import sys
import time
import wave

import pytest

from local_music_engine import auto_quality
from local_music_engine.auto_quality import _quality_rank, compare_engine_metas, listening_points, prepare_payload, quality_policy
from local_music_engine.jobs import recover_project
from local_music_engine.storage import ProjectStore
from local_music_engine.workflow import (_frozen_generation_payload, export_selected, generate_candidates, resume_latest_batch,
                                         revise_inputs)
from test_workflow import FakeAceClient

LYRICS = "[Verse]\n창가에 작은 별빛\n고요한 밤이 감싸\n[Chorus]\n함께 걸어가 내 곁에\n새로운 아침 올 때까지"
SUNG = "창가에 작은 별빛 고요한 밤이 감싸 함께 걸어가 내 곁에 새로운 아침 올 때까지"


class Reports:
    def __init__(self, texts=None, *, error=False, interrupt=False):
        self.texts = texts or [SUNG]
        self.calls = 0
        self.error = error
        self.interrupt = interrupt

    def analyze(self, audio: Path, *, progress=None):
        self.calls += 1
        if self.interrupt:
            raise KeyboardInterrupt
        if self.error:
            raise RuntimeError("inspector failed")
        text = self.texts[min(self.calls - 1, len(self.texts) - 1)]
        return {"available": True, "reliable": True, "text": text, "language": "ko",
                "segments": [{"start": 0.1, "end": 9.0, "text": text, "avg_logprob": -0.1, "no_speech_prob": 0.0}],
                "loudness": {"status": "unknown"}}


@pytest.fixture
def song(tmp_path):
    FakeAceClient.submitted = []
    FakeAceClient.fail_seeds = set()
    return ProjectStore.initialize(tmp_path / "song", title="품질 검사", lyrics=LYRICS,
                                   style_prompt="Korean acoustic pop", target_duration_seconds=10)


def generate(store, *, backend=None, seeds=(1,), **kwargs):
    return generate_candidates(store.root, seeds=seeds, client_factory=FakeAceClient,
                               quality=quality_policy(), quality_backend=backend or Reports(), **kwargs, engine="ace-step")


def test_one_click_keeps_raw_audio_and_creates_separate_unreviewed_recommendation(song, tmp_path):
    result = generate(song)
    assert len(FakeAceClient.submitted) == 1
    assert len(result["candidateIds"]) == 1
    project = song.load()
    raw, finished = project["candidates"]
    assert finished["parentCandidateId"] == raw["candidateId"]
    assert raw["artifactId"] != finished["artifactId"]
    assert raw["quality"]["preferred"] is False
    assert finished["quality"]["preferred"] is True
    assert project["selectedCandidateId"] is None
    assert project["recommendedCandidateId"] == result["recommendedCandidateId"] == finished["candidateId"]
    assert {candidate["humanReview"]["status"] for candidate in project["candidates"]} == {"unreviewed"}
    assert all(song.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    exported = export_selected(song.root, candidate_id=finished["candidateId"], output=tmp_path / "song.wav")
    assert exported["sha256"] == project["artifacts"][-1]["sha256"]
    assert song.load()["selectedCandidateId"] is None


def test_trustworthy_lyric_mismatch_regenerates_and_stops_when_coverage_is_good(song):
    backend = Reports(["바다는 차갑고 어둠은 깊게 번진다", SUNG])
    result = generate(song, backend=backend)
    assert len(FakeAceClient.submitted) == backend.calls == 2
    assert len(set(FakeAceClient.submitted)) == 2
    chosen = song.find_by_id(song.load(), "candidates", "candidateId", result["candidateIds"][0])
    assert chosen["quality"]["attempt"] == 2
    assert chosen["quality"]["lyrics"]["status"] == "pass"
    assert chosen["quality"]["attemptsUsed"] == 2


def test_attempt_budget_is_four_total_and_best_effort_remains_available(song):
    result = generate(song, backend=Reports(["바다는 차갑고 어둠은 깊게 번진다"]))
    assert len(FakeAceClient.submitted) == 4
    project = song.load()
    assert len(result["candidateIds"]) == 1
    assert len(result["attemptCandidateIds"]) == 4
    assert len(project["candidates"]) == 5
    winner = song.find_by_id(project, "candidates", "candidateId", result["candidateIds"][0])
    assert winner["quality"]["status"] == "attention"
    assert winner["humanReview"]["status"] == "unreviewed"


def test_engine_metas_compare_only_what_was_requested_and_reported():
    requested = {"bpm": 98.0, "key_scale": "F minor", "time_signature": "4"}
    assert compare_engine_metas(requested, {"bpm": 98, "keyscale": "f  Minor", "timesignature": "4/4"})["status"] == "matched"
    assert compare_engine_metas(requested, None) is None
    assert compare_engine_metas(requested, {"engine": "minimax-music3", "duration": 30.0}) is None  # reports none of them
    assert compare_engine_metas({"bpm": None, "key_scale": ""}, {"bpm": 88, "keyscale": "E major"}) is None
    planner_chose = compare_engine_metas(requested, {"bpm": 88, "keyscale": "F minor", "timesignature": "4"})
    assert planner_chose["status"] == "needs_review"
    assert [(item["check"], item["observed"], item["retryEligible"]) for item in planner_chose["findings"]] == [
        ("engine_meta_mismatch", {"field": "bpm", "requested": 98.0, "reported": 88}, False)]
    never_arrived = compare_engine_metas(requested, {"bpm": "N/A", "keyscale": "F minor", "timesignature": "4"})
    assert never_arrived["findings"][0]["observed"] == {"field": "bpm", "requested": 98.0, "reported": None}


def test_engine_that_ignored_the_requested_tempo_is_flagged_once_without_regenerating(tmp_path):
    class PlannerChoseTempo(FakeAceClient):
        def wait(self, task_id, **kwargs):
            return {**super().wait(task_id, **kwargs), "metas": {"bpm": 88, "keyscale": "F minor", "timesignature": "4"}}

    FakeAceClient.submitted = []
    store = ProjectStore.initialize(tmp_path / "song", title="템포", lyrics=LYRICS, style_prompt="Korean acoustic pop",
                                    target_duration_seconds=10, bpm=98, key_scale="F minor", time_signature="4")
    generate_candidates(store.root, seeds=[1], client_factory=PlannerChoseTempo, quality=quality_policy(),
                        quality_backend=Reports(), engine="ace-step")
    project = store.load()
    assert FakeAceClient.submitted == [1]
    for candidate in project["candidates"]:  # the raw attempt and its finished copy
        assert candidate["quality"]["status"] == "attention" and candidate["quality"]["retryReasons"] == []
        assert candidate["quality"]["engine"]["fields"][0] == {"field": "bpm", "requested": 98, "reported": 88, "matched": False}
        checks = [store.find_by_id(project, "findings", "findingId", item)["check"] for item in candidate["findingIds"]]
        assert checks.count("engine_meta_mismatch") == 1
    older_plan = {key: value for key, value in quality_policy().items() if key != "engineMetas"}
    generate_candidates(store.root, seeds=[2], client_factory=PlannerChoseTempo, quality=older_plan,
                        quality_backend=Reports(), engine="ace-step")
    assert all("engine" not in candidate["quality"] for candidate in store.load()["candidates"][2:])


def test_inspector_failure_is_unknown_and_cannot_cause_repeated_generation(song):
    result = generate(song, backend=Reports(error=True))
    assert len(FakeAceClient.submitted) == 1
    winner = song.find_by_id(song.load(), "candidates", "candidateId", result["candidateIds"][0])
    assert winner["quality"]["lyrics"]["status"] == "unknown"
    assert winner["quality"]["retryReasons"] == []
    # The failed inspector is named, and what the PCM check measured stays in the report.
    assert winner["quality"]["inspectionErrors"] == [{"stage": "lyrics", "error": "RuntimeError: inspector failed"}]
    assert winner["quality"]["audio"]["metrics"]["durationSeconds"] == 10
    assert result["status"] == "succeeded" and result["failures"] == []


class SilentClient(FakeAceClient):
    def download(self, file_path, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(bytes(2 * 8000 * 10))


def test_a_failed_inspector_does_not_erase_a_measured_audio_defect(song):
    result = generate_candidates(song.root, seeds=[1], client_factory=SilentClient, quality=quality_policy(),
                                 quality_backend=Reports(error=True), engine="ace-step")
    assert len(FakeAceClient.submitted) == 4  # silence is measured without the inspector, so it still regenerates
    winner = song.find_by_id(song.load(), "candidates", "candidateId", result["candidateIds"][0])
    assert winner["quality"]["retryReasons"] == ["digital_silence"]
    assert winner["quality"]["status"] == "attention"


def test_attempts_with_fewer_places_to_check_rank_first_and_unread_lyrics_rank_between():
    read = {"audio": {"retryEligible": False, "technicalScore": 100, "findings": []},
            "lyrics": {"status": "pass", "orderedCoverage": 0.9}, "retryReasons": []}
    flagged = {**read, "rhythm": {"findings": [{"check": "mix_dropout_suspected", "severity": "warning"}]}}
    section_change = {**read, "rhythm": {"findings": [{"check": "loudness_shift", "severity": "warning"},
                                                      {"check": "tempo_ambiguity", "severity": "warning"}]}}
    assert listening_points(flagged) == 1 and listening_points(section_change) == 0
    assert _quality_rank(read) > _quality_rank(flagged)
    assert _quality_rank(read) == _quality_rank(section_change)
    # A slightly better transcript does not outweigh a measured dropout.
    assert _quality_rank(read) > _quality_rank({**flagged, "lyrics": {"status": "pass", "orderedCoverage": 0.99}})
    unread = {**read, "lyrics": {"status": "unknown"}}
    badly_sung = {**read, "lyrics": {"status": "warning", "orderedCoverage": 0.4}, "retryReasons": ["low_ordered_lyric_coverage"]}
    assert _quality_rank(read) > _quality_rank(unread) > _quality_rank(badly_sung)


def test_finishing_failure_is_reported_and_resume_repeats_only_that_step(song, monkeypatch):
    original = auto_quality.finish_audio
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return original(*args, **kwargs)

    monkeypatch.setattr(auto_quality, "finish_audio", fail_once)
    backend = Reports()
    first = generate(song, backend=backend)
    assert first["status"] == "partial"
    assert first["failures"] == [{"seed": "1", "stage": "audio_finishing", "error": "OSError: disk hiccup"}]
    kept = song.find_by_id(song.load(), "candidates", "candidateId", first["candidateIds"][0])
    assert kept["quality"]["processing"] is None and kept["quality"]["preferred"] is True
    assert kept["quality"]["finishingError"] == "OSError: disk hiccup"

    again = resume_latest_batch(song.root, client_factory=FakeAceClient, quality_backend=backend, engine="ace-step")
    assert again["status"] == "succeeded" and again["failures"] == []
    assert len(FakeAceClient.submitted) == backend.calls == 1  # neither generated nor inspected again
    project = song.load()
    finished = song.find_by_id(project, "candidates", "candidateId", again["candidateIds"][0])
    assert again["newCandidateIds"] == [finished["candidateId"]] and again["reusedCandidateIds"] == []
    assert finished["parentCandidateId"] == kept["candidateId"] and finished["quality"]["processing"]
    assert "finishingError" not in finished["quality"]
    raw = song.find_by_id(project, "candidates", "candidateId", kept["candidateId"])
    assert raw["quality"]["preferred"] is False and "finishingError" not in raw["quality"]
    assert project["recommendedCandidateId"] == finished["candidateId"]


@pytest.mark.parametrize("rules", [[], ["steady-groove", "repeated-harmony", "focused-arrangement", "clear-vocal",
                                        "clean-production", "simple-structure"]])
def test_the_default_chord_loop_rule_is_not_a_request_for_loop_audio(tmp_path, rules):
    dense = "[Verse]\n" + "가나다라마바사 " * 50
    store = ProjectStore.initialize(tmp_path / "song", title="밀도", lyrics=dense, style_prompt="Korean pop",
                                    target_duration_seconds=10, production_rules={"version": 1, "presetId": "upbeat-pop", "ruleIds": rules})
    payload = _frozen_generation_payload(store.load(), seed=1, model="acestep-v15-xl-turbo", lm_model="acestep-5Hz-lm-4B")
    assert ("chord loop" in payload["prompt"]) == bool(rules)
    prepared = prepare_payload(payload)
    assert prepared["audio_duration"] == 80
    assert {"outro_structure_tag_added", "duration_extended_for_dense_lyrics"} <= set(prepared["qualityPreparation"]["changes"])


@pytest.mark.parametrize("style,loops", [("warm piano chord loop, Korean pop", False), ("피아노 루프, 발라드", False),
                                         ("seamless loop for a game menu", True), ("게임 배경 루프", True)])
def test_only_a_request_for_loop_audio_skips_the_outro(style, loops):
    prepared = prepare_payload({"lyrics": LYRICS, "prompt": style, "audio_duration": 30, "seed": 1})
    assert ("[Outro]" not in prepared["lyrics"]) == loops


def test_finished_resume_needs_no_engine_or_inspector_even_if_raw_backup_is_missing(song):
    first = generate(song)
    project = song.load()
    song.resolve_artifact(project["artifacts"][0]).unlink()

    class Offline(FakeAceClient):
        def health(self):
            raise AssertionError("a finished resume must not use the engine")

    class NoInspector:
        def analyze(self, *args, **kwargs):
            raise AssertionError("a finished resume must not repeat the inspector")

    again = resume_latest_batch(song.root, client_factory=Offline, quality_backend=NoInspector(), engine="ace-step")
    assert again["reusedCandidateIds"] == first["candidateIds"]
    assert again["newCandidateIds"] == []
    assert len(FakeAceClient.submitted) == 1


def test_cancelled_inspection_resumes_verified_audio_with_its_original_inputs(song):
    with pytest.raises(KeyboardInterrupt):
        generate(song, backend=Reports(interrupt=True))
    original = song.load()["requests"][0]["parameters"]
    revise_inputs(song.root, lyrics="다음 곡 가사", style_prompt="jazz", duration_seconds=60)
    result = resume_latest_batch(song.root, client_factory=FakeAceClient, quality_backend=Reports(), engine="ace-step")
    assert len(FakeAceClient.submitted) == 1
    assert len(result["newCandidateIds"]) == 1
    project = song.load()
    assert project["requests"][-1]["parameters"]["lyrics"] == original["lyrics"]
    assert project["inputs"]["lyricsOriginal"] == "다음 곡 가사"
    assert not any(job["status"] in {"running", "queued"} for job in project["jobs"])


def test_failed_submissions_are_counted_across_all_resume_attempts(song):
    class Failed(FakeAceClient):
        def wait(self, *args, **kwargs):
            raise RuntimeError("remote task finished with failure")

    first = generate_candidates(song.root, seeds=[1], client_factory=Failed, quality=quality_policy(), quality_backend=Reports(), engine="ace-step")
    assert first["status"] == "failed"
    assert len(FakeAceClient.submitted) == 4
    for _ in range(3):
        result = resume_latest_batch(song.root, client_factory=Failed, quality_backend=Reports(), engine="ace-step")
        assert result["status"] == "failed"
    assert len(FakeAceClient.submitted) == 4


def test_timeout_does_not_automatically_submit_more_remote_work(song):
    class TimedOut(FakeAceClient):
        def wait(self, *args, **kwargs):
            raise TimeoutError("remote task might still be computing")

    result = generate_candidates(song.root, seeds=[1, 2], client_factory=TimedOut, quality=quality_policy(), quality_backend=Reports(), engine="ace-step")
    assert result["status"] == "failed"
    assert len(FakeAceClient.submitted) == 1


def test_a_failed_requested_version_does_not_hide_successful_versions(song):
    class SecondFails(FakeAceClient):
        def wait(self, *args, **kwargs):
            if self.seed != 1:
                raise RuntimeError("remote task finished with failure")
            return super().wait(*args, **kwargs)

    result = generate_candidates(song.root, seeds=[1, 2], client_factory=SecondFails, quality=quality_policy(), quality_backend=Reports(), engine="ace-step")
    assert result["status"] == "partial"
    assert len(result["candidateIds"]) == 1
    assert len(result["failures"]) == 1
    assert song.load()["recommendedCandidateId"] == result["candidateIds"][0]


@pytest.mark.parametrize("committed", [False, True])
def test_finishing_save_fault_preserves_raw_audio_and_only_cleans_uncommitted_files(song, monkeypatch, committed):
    original_save = ProjectStore.save
    failed = False

    def fail_final(self, project):
        nonlocal failed
        job = project["jobs"][-1]
        if not failed and job["kind"] == "audio-finish" and job["status"] == "succeeded":
            failed = True
            if committed:
                original_save(self, project)
            raise OSError("final finish manifest write failed")
        return original_save(self, project)

    monkeypatch.setattr(ProjectStore, "save", fail_final)
    result = generate(song)
    project = song.load()
    assert len(result["candidateIds"]) == 1
    assert all(song.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    assert len(list((song.root / "artifacts/finished").glob("*.wav"))) == int(committed)
    assert not list((song.root / "artifacts/finished").glob(".*.tmp"))
    chosen = song.find_by_id(project, "candidates", "candidateId", result["candidateIds"][0])
    assert bool(chosen["quality"].get("processing")) == committed


@pytest.mark.parametrize("kind", ["generate-candidate", "quality-check"])
def test_commit_then_save_error_cannot_repeat_inference_or_erase_completed_check(song, monkeypatch, kind):
    original_save = ProjectStore.save
    failed = False

    def fail_after_commit(self, project):
        nonlocal failed
        job = project["jobs"][-1]
        if not failed and job["kind"] == kind and job["status"] == "succeeded":
            failed = True
            original_save(self, project)
            raise OSError("directory fsync failed after replace")
        return original_save(self, project)

    monkeypatch.setattr(ProjectStore, "save", fail_after_commit)
    result = generate(song)
    assert len(FakeAceClient.submitted) == 1
    chosen = song.find_by_id(song.load(), "candidates", "candidateId", result["candidateIds"][0])
    assert chosen["quality"]["lyrics"]["status"] == "pass"


def test_attempt_count_includes_failed_generations_before_a_good_version(song):
    class TwoFailures(FakeAceClient):
        def wait(self, *args, **kwargs):
            if len(self.submitted) < 3:
                raise RuntimeError("remote task finished with failure")
            return super().wait(*args, **kwargs)

    result = generate_candidates(song.root, seeds=[1], client_factory=TwoFailures, quality=quality_policy(), quality_backend=Reports(), engine="ace-step")
    assert result["status"] == "succeeded" and result["failures"] == []
    chosen = song.find_by_id(song.load(), "candidates", "candidateId", result["candidateIds"][0])
    assert chosen["quality"]["attempt"] == chosen["quality"]["attemptsUsed"] == 3
    assert chosen["quality"]["assessedAttempts"] == 1


def test_dense_lyrics_can_extend_duration_without_rewriting_project_inputs():
    original = "[Verse]\n" + "가나다라마바사 " * 50
    payload = {"lyrics": original, "prompt": "Korean pop", "audio_duration": 30, "seed": 1}
    prepared = prepare_payload(payload)
    assert prepared["audio_duration"] > 30
    assert prepared["qualityPreparation"]["requestedDurationSeconds"] == 30
    assert prepared["qualityPreparation"]["lyricsOriginal"] == original
    assert "".join(prepared["lyrics"].replace("[Outro]", "").split()) == "".join(original.split())
    assert payload["audio_duration"] == 30 and payload["lyrics"] == original


def test_loop_music_does_not_gain_an_outro_tag():
    payload = {"lyrics": LYRICS, "prompt": "seamless loop, Korean pop", "audio_duration": 30, "seed": 1}
    assert "[Outro]" not in prepare_payload(payload)["lyrics"]


@pytest.mark.parametrize("style", ["fast rap", "seamless loop", "hard cut ending"])
def test_explicit_fast_or_loop_intent_does_not_silently_change_duration(style):
    payload = {"lyrics": "가나다라마바사 " * 100, "prompt": style, "audio_duration": 30, "seed": 1}
    assert prepare_payload(payload)["audio_duration"] == 30


@pytest.mark.parametrize("committed", [False, True])
def test_staging_ownership_save_fault_does_not_leave_an_untracked_temporary(song, monkeypatch, committed):
    original_save = ProjectStore.save
    failed = False

    def fail_ownership(self, project):
        nonlocal failed
        job = project["jobs"][-1]
        if not failed and job["kind"] == "audio-finish" and job["status"] == "running" and job.get("ownedOutputs"):
            failed = True
            if committed:
                original_save(self, project)
            raise OSError("staging ownership could not be saved")
        return original_save(self, project)

    monkeypatch.setattr(ProjectStore, "save", fail_ownership)
    result = generate(song)
    assert len(result["candidateIds"]) == 1
    assert not list((song.root / "artifacts/finished").iterdir())
    assert all(song.verify_artifact(artifact)[0] for artifact in song.load()["artifacts"])


def test_invalid_quality_snapshot_fails_before_mutating_resume_history(song):
    result = generate(song)
    with song.transaction() as project:
        batch = song.find_by_id(project, "jobs", "jobId", result["batchJobId"])
        batch["parameters"]["qualityPlan"][0].append(deepcopy(batch["parameters"]["qualityPlan"][0][0]))
    before = song.manifest_path.read_bytes()
    with pytest.raises(ValueError):
        resume_latest_batch(song.root, client_factory=FakeAceClient, quality_backend=Reports(), engine="ace-step")
    assert song.manifest_path.read_bytes() == before


def _kill_process_at_finishing_commit(store: ProjectStore, *, committed: bool):
    """Use actual process death while the generation/manifest locks are owned."""

    marker = store.root / "finishing-commit-paused"
    submissions = store.root / "test-submissions"
    script = '''
import signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from test_auto_quality import Reports
from test_workflow import FakeAceClient
from local_music_engine.auto_quality import quality_policy
from local_music_engine.storage import ProjectStore
from local_music_engine.workflow import generate_candidates
root = Path(sys.argv[1])
committed = sys.argv[3] == "committed"
original_save = ProjectStore.save
def pause_at_success(self, project):
    job = project["jobs"][-1]
    if job["kind"] == "audio-finish" and job["status"] == "succeeded":
        if committed:
            original_save(self, project)
        (root / "finishing-commit-paused").touch()
        signal.pause()
    return original_save(self, project)
class RecordedClient(FakeAceClient):
    def submit(self, request, source_audio=None):
        task = super().submit(request, source_audio)
        with (root / "test-submissions").open("a", encoding="utf-8") as handle:
            handle.write(str(request["seed"]) + "\\n")
        return task
ProjectStore.save = pause_at_success
generate_candidates(root, seeds=[1], client_factory=RecordedClient,
                    quality=quality_policy(), quality_backend=Reports(), engine="ace-step")
'''
    child = subprocess.Popen([
        sys.executable, "-c", script, str(store.root), str(Path(__file__).parent),
        "committed" if committed else "uncommitted",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                _, stderr = child.communicate(timeout=1)
                pytest.fail(f"finishing subprocess exited before checkpoint: {stderr.decode()}")
            time.sleep(0.02)
        assert marker.exists(), "finishing subprocess did not reach checkpoint"
        assert store.generation_active()
        project = store.load()
        finish_job = next(job for job in reversed(project["jobs"]) if job["kind"] == "audio-finish")
        assert finish_job["status"] == ("succeeded" if committed else "running")
        assert len(finish_job["ownedOutputs"]) == 1
        owned = finish_job["ownedOutputs"][0]
        assert Path(owned["path"]).is_file() and Path(owned["temporaryPath"]).is_file()
        assert Path(owned["path"]).samefile(owned["temporaryPath"])
        assert submissions.read_text().splitlines() == ["1"]
        child.kill()
        child.wait(timeout=5)
        assert child.returncode == -signal.SIGKILL
        assert not store.generation_active()
        return project, finish_job, submissions
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


class NoAdditionalInference(FakeAceClient):
    def health(self):
        pytest.fail("resume must reuse the completed raw inference")

    def submit(self, *args, **kwargs):
        pytest.fail("resume must not submit another inference")


class NoAdditionalInspection:
    def analyze(self, *args, **kwargs):
        pytest.fail("resume must reuse the completed raw inspection")


def test_sigkill_after_finished_wav_publication_recovers_and_resumes_raw_without_inference(song):
    before, finish_job, submissions = _kill_process_at_finishing_commit(song, committed=False)
    raw = before["candidates"][0]
    assert len(before["candidates"]) == len(before["artifacts"]) == 1
    assert raw["quality"]["complete"]
    source_artifact = before["artifacts"][0]
    owned = finish_job["ownedOutputs"][0]
    recovered = recover_project(song.root)
    assert finish_job["jobId"] in recovered["interruptedJobIds"]
    assert not Path(owned["path"]).exists() and not Path(owned["temporaryPath"]).exists()
    assert song.verify_artifact(source_artifact) == (True, "ok")
    result = resume_latest_batch(song.root, client_factory=NoAdditionalInference,
                                quality_backend=NoAdditionalInspection(), engine="ace-step")
    project = song.load()
    finished = song.find_by_id(project, "candidates", "candidateId", result["candidateIds"][0])
    assert finished["parentCandidateId"] == raw["candidateId"]
    assert finished["quality"]["processing"]["sourceCandidateId"] == raw["candidateId"]
    assert result["newCandidateIds"] == [finished["candidateId"]]
    assert submissions.read_text().splitlines() == ["1"] and FakeAceClient.submitted == []
    assert len(project["candidates"]) == 2
    assert _listening_copy_of(project, finished)["kind"] == "listening-audio"
    assert all(song.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    assert not list((song.root / "artifacts/finished").glob(".*.tmp"))
    assert not any(job["status"] in {"queued", "running", "cancelling"} for job in project["jobs"])
    assert {candidate["humanReview"]["status"] for candidate in project["candidates"]} == {"unreviewed"}


def _listening_copy_of(project, candidate):
    copies = [artifact for artifact in project["artifacts"] if artifact.get("sourceArtifactId") == candidate["artifactId"]]
    assert len(copies) == 1 and len(project["artifacts"]) == len(project["candidates"]) + 1
    return copies[0]


def test_sigkill_after_finished_success_commit_preserves_output_cleans_anchor_and_reuses_finished(song):
    before, finish_job, submissions = _kill_process_at_finishing_commit(song, committed=True)
    assert len(before["candidates"]) == len(before["artifacts"]) == 2
    finished = before["candidates"][-1]
    finished_artifact = before["artifacts"][-1]
    owned = finish_job["ownedOutputs"][0]
    recovered = recover_project(song.root)
    assert finish_job["jobId"] not in recovered["interruptedJobIds"]
    assert Path(owned["path"]).is_file() and not Path(owned["temporaryPath"]).exists()
    assert song.verify_artifact(finished_artifact) == (True, "ok")
    result = resume_latest_batch(song.root, client_factory=NoAdditionalInference,
                                quality_backend=NoAdditionalInspection(), engine="ace-step")
    project = song.load()
    assert result["candidateIds"] == result["reusedCandidateIds"] == [finished["candidateId"]]
    assert result["newCandidateIds"] == []
    assert submissions.read_text().splitlines() == ["1"] and FakeAceClient.submitted == []
    assert len(project["candidates"]) == 2
    # The kill came before mastering; the resume masters the reused version without finishing it again.
    assert _listening_copy_of(project, finished)["mastering"]["driveInherited"] is False
    assert len([job for job in project["jobs"] if job["kind"] == "audio-finish"]) == 1
    assert all(song.verify_artifact(artifact)[0] for artifact in project["artifacts"])
    assert not list((song.root / "artifacts/finished").glob(".*.tmp"))
    assert not any(job["status"] in {"queued", "running", "cancelling"} for job in project["jobs"])
    assert {candidate["humanReview"]["status"] for candidate in project["candidates"]} == {"unreviewed"}


VERSE_LINES = "silver morning carries quiet rivers\nunder open skies tonight we wander\npaper lanterns drift on sleeping water"
CHORUS_LINES = "hold me closer when the neon fades away"
SECTIONS = f"[Verse]\n{VERSE_LINES}\n[Chorus]\n{CHORUS_LINES}"


class SeveralReadings:
    """The first pass loses the verse to its opening window; further readings settle it."""

    def __init__(self, later_hear_verse):
        self.later_hear_verse, self.spans = later_hear_verse, []

    @staticmethod
    def reading(*lines):
        return {"available": True, "reliable": True, "language": "en", "loudness": {"status": "unknown"},
                "text": " ".join(text for _, _, text in lines),
                "segments": [{"start": start, "end": end, "text": text, "avg_logprob": -0.1, "no_speech_prob": 0.0} for start, end, text in lines]}

    def analyze(self, audio, *, progress=None):
        return {**self.reading((0.5, 3.0, "oh oh oh"), (5.0, 9.5, CHORUS_LINES)), "windowAnchorSeconds": 0.0}

    def transcribe_span(self, audio, *, start_seconds, end_seconds, language=None):
        self.spans.append((start_seconds, end_seconds, language))
        verse = [(1.0, 4.5, VERSE_LINES)] if self.later_hear_verse else []
        return self.reading(*verse, (5.0, 9.5, CHORUS_LINES))


@pytest.mark.parametrize("later_hear_verse", [True, False])
def test_a_verse_lost_by_the_first_reading_is_read_again_before_any_regeneration(tmp_path, later_hear_verse):
    FakeAceClient.submitted = []
    store = ProjectStore.initialize(tmp_path / "song", title="재확인", lyrics=SECTIONS, style_prompt="pop", target_duration_seconds=10)
    backend = SeveralReadings(later_hear_verse)
    result = generate(store, backend=backend)
    project = store.load()
    winner = store.find_by_id(project, "candidates", "candidateId", result["candidateIds"][0])
    assert backend.spans[:3] == [(0.0, 9.5, "en"), (1.583, 9.5, "en"), (3.167, 9.5, "en")]  # three other starting points
    if later_hear_verse:
        assert len(FakeAceClient.submitted) == 1 and winner["quality"]["lyrics"]["status"] == "pass"
        assert winner["quality"]["lyrics"]["rereads"][0]["adopted"]
    else:
        assert len(FakeAceClient.submitted) == 4  # every attempt was heard without its verse, three times over
        assert "written_section_missing" in winner["quality"]["retryReasons"]
        checks = [store.find_by_id(project, "findings", "findingId", item) for item in winner["findingIds"]]
        missing = [item for item in checks if item["check"] == "lyric_lines_missing"]
        assert len(missing) == 1 and missing[0]["startSeconds"] == 0.0 and missing[0]["observed"]["section"] == "Verse"
