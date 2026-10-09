"""Controlled audio files retain provenance without inventing clean-source labels."""

import importlib.util
import json
from pathlib import Path
import wave

import numpy as np
import pytest

from local_music_engine.percussion_analysis import analyze_percussion
from local_music_engine.rhythm_evaluation import validate_manifest
from local_music_engine.rhythm_intent import load_rhythm_intent
from local_music_engine.storage import ProjectStore, sha256_file

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_rhythm_evaluation.py"
spec = importlib.util.spec_from_file_location("rhythm_corpus_builder", SCRIPT)
builder = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(builder)


def write_source(path, *, seconds=30, silence=False):
    rate = 8000
    times = np.arange(round(seconds * rate)) / rate
    samples = np.zeros((len(times), 2))
    if not silence:
        bed = 0.02 * np.sin(2 * np.pi * 220 * times)
        samples[:, 0] = bed
        samples[:, 1] = -0.6 * bed
        attack_times = np.arange(400) / rate
        attack = np.random.default_rng(18).normal(size=400) * 0.17 * np.exp(-attack_times * 100)
        attack += 0.12 * np.sin(2 * np.pi * 130 * attack_times) * np.exp(-attack_times * 70)
        for start in np.arange(0.3, seconds - 0.1, 0.5):
            first = round(start * rate)
            stop = min(len(samples), first + len(attack))
            samples[first:stop, 0] += attack[:stop - first]
            samples[first:stop, 1] -= attack[:stop - first] * 0.6
    data = np.rint(samples * 32768).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(data)
    return data


def pcm(path):
    with wave.open(str(path), "rb") as audio:
        return audio.readframes(audio.getnframes()), audio.getnframes(), audio.getnchannels(), audio.getframerate()


@pytest.fixture
def corpus(tmp_path):
    source = tmp_path / "source.wav"
    source_pcm = write_source(source)
    before = source.read_bytes()
    result = builder.build_corpus(source, tmp_path / "corpus", split="calibration", source_family="known-pulses",
                                  strengths_ms=(130,), durations_seconds=(4,), kinds=("alternating", "burst"))
    assert source.read_bytes() == before
    manifest = json.loads(Path(result["manifestPath"]).read_text())
    provenance = json.loads(Path(result["provenancePath"]).read_text())
    return source, source_pcm, result, manifest, provenance


def test_corpus_manifest_is_source_bound_and_natural_baseline_controls_stay_unlabelled(corpus):
    source, source_pcm, result, manifest, provenance = corpus
    normalized = validate_manifest(manifest)
    assert result["caseCount"] == len(normalized["cases"]) == 15
    assert provenance["newEngineGeneration"] is provenance["newModelInference"] is False
    assert provenance["sourceArtifactSha256"] == sha256_file(source)
    for case in manifest["cases"]:
        assert case["parentArtifactSha256"] == sha256_file(source)
        assert case["sourceFamily"] == "known-pulses"
        path = Path(case["audioPath"])
        assert path.stat().st_size == case["audioBytes"]
        assert sha256_file(path) == case["sourceArtifactSha256"]
        data, frames, channels, rate = pcm(path)
        assert (frames, channels, rate) == (240000, 2, 8000)
        assert len(data) == len(source_pcm)
        assert all(annotation["category"] != "backing_dropout" for annotation in case["annotations"])
        if case["caseId"].endswith(("baseline", "uniform-gain", "smooth-fade")):
            assert case["reviewedRanges"] == case["annotations"] == []
    baseline, = [case for case in manifest["cases"] if case["caseId"].endswith("baseline")]
    assert pcm(Path(baseline["audioPath"]))[0] == source_pcm


def test_actual_multichannel_pcm_warp_has_measured_shifted_attacks_and_unchanged_flanks(corpus):
    _, original, _, manifest, provenance = corpus
    case, = [case for case in manifest["cases"] if "alternating" in case["caseId"]]
    transform, = [item for item in provenance["transforms"] if item["caseId"] == case["caseId"]]
    changed = pcm(Path(case["audioPath"]))[0]
    first, stop = [round(time * 8000) for time in transform["regionSeconds"]]
    assert changed[:first * 4] == original[:first * 4]
    assert changed[stop * 4:] == original[stop * 4:]
    assert changed[first * 4:stop * 4] != original[first * 4:stop * 4]
    assert transform["flankContext"]["eightStablePulsesOnEachFlank"] is True
    assert np.all(np.diff(transform["outputTimeKnotsSeconds"]) > 0)
    measured = analyze_percussion(Path(case["audioPath"]))
    attacks = measured["onsetTimesSeconds"]
    displaced = [(source, target) for source, target in transform["measuredSourceOnsetMapping"] if abs(source - target) >= 0.1]
    assert len(displaced) >= 3
    assert all(min(abs(observed - target) for observed in attacks) <= 0.04 for _, target in displaced)
    decoded = np.frombuffer(changed, "<i2").reshape(-1, 2)
    active = np.abs(decoded[:, 0]) > 100
    assert np.max(np.abs(decoded[active, 1] + 0.6 * decoded[active, 0])) <= 1.5


def bed_frequencies(path, attack_times):
    """Dominant frequency of the sustained 220 Hz bed in the quiet stretch after each attack."""
    decoded = np.frombuffer(pcm(Path(path))[0], "<i2").reshape(-1, 2)[:, 0].astype(float)
    found = []
    for time in attack_times:
        window = decoded[round((time + 0.14) * 8000):round((time + 0.34) * 8000)]
        spectrum = np.abs(np.fft.rfft(window * np.hanning(len(window)), 16000))
        found.append(float(np.argmax(spectrum[100:]) + 100) * 8000 / 16000)
    return np.array(found)


def test_overlap_add_time_map_moves_attacks_without_changing_pitch(tmp_path, corpus):
    source, original, _, _, resampled = corpus
    assert resampled["timeMethod"] == "resample"
    result = builder.build_corpus(source, tmp_path / "pitch-preserved", split="calibration", source_family="known-pulses",
                                  strengths_ms=(130,), durations_seconds=(4,), kinds=("alternating",), time_method="overlap_add")
    provenance = json.loads(Path(result["provenancePath"]).read_text())
    assert provenance["timeMethod"] == "overlap_add"
    transform, = [item for item in provenance["transforms"] if item["kind"] == "controlled_time_warp"]
    assert transform["timeMethod"] == "overlap_add"
    changed = pcm(Path(transform["audioPath"]))[0]
    first, stop = [round(time * 8000) for time in transform["regionSeconds"]]
    assert changed[:first * 4] == original[:first * 4]
    assert changed[stop * 4:] == original[stop * 4:]
    assert changed[first * 4:stop * 4] != original[first * 4:stop * 4]
    # Attacks arrive where the map sends them, to within the 10 ms grain tolerance.
    attacks = analyze_percussion(Path(transform["audioPath"]))["onsetTimesSeconds"]
    displaced = [target for source_time, target in transform["measuredSourceOnsetMapping"] if abs(source_time - target) >= 0.1]
    assert len(displaced) >= 3
    assert all(min(abs(observed - target) for observed in attacks) <= 0.05 for target in displaced)
    # The same map by resampling bends the sustained tone; overlap-add leaves it at 220 Hz.
    other, = [item for item in resampled["transforms"] if item["kind"] == "controlled_time_warp" and item["pattern"] == "alternating"]
    assert other["inputTimeKnotsSeconds"] == transform["inputTimeKnotsSeconds"]
    assert np.max(np.abs(bed_frequencies(transform["audioPath"], displaced) - 220)) <= 8
    assert np.max(np.abs(bed_frequencies(other["audioPath"], displaced) - 220)) >= 30


def test_unknown_time_method_is_rejected_before_any_file_is_written(tmp_path):
    source = tmp_path / "source.wav"
    write_source(source)
    with pytest.raises(ValueError, match="time method"):
        builder.build_corpus(source, tmp_path / "corpus", split="calibration", source_family="known-pulses",
                             time_method="phase_vocoder")
    assert not (tmp_path / "corpus").exists()


def test_burst_truth_covers_only_affected_map_segments_and_pcm(corpus):
    _, original, _, manifest, provenance = corpus
    case, = [case for case in manifest["cases"] if "-burst-" in case["caseId"]]
    transform, = [item for item in provenance["transforms"] if item["caseId"] == case["caseId"]]
    source, target = np.array(transform["inputTimeKnotsSeconds"]), np.array(transform["outputTimeKnotsSeconds"])
    displaced = np.flatnonzero(source != target)
    expected = [source[displaced[0] - 1], source[displaced[-1] + 1]]
    annotation = case["annotations"][0]
    assert [annotation["startSeconds"], annotation["endSeconds"]] == expected
    assert transform["authoredRegionSeconds"] == transform["regionSeconds"]
    assert transform["timeMapSupportSeconds"] == [expected]
    assert expected[1] - expected[0] < transform["regionSeconds"][1] - transform["regionSeconds"][0]
    assert case["reviewedRanges"] == [{"startSeconds": expected[0], "endSeconds": expected[1], "categories": ["beat_timing"]}]
    changed = pcm(Path(case["audioPath"]))[0]
    first, stop = [round(t * 8000) for t in expected]
    assert changed[:first * 4] == original[:first * 4]
    assert changed[stop * 4:] == original[stop * 4:]
    assert changed[first * 4:stop * 4] != original[first * 4:stop * 4]


def test_digital_gap_pairs_have_exact_known_pcm_interventions_and_source_bound_rest_plans(corpus):
    _, original, _, manifest, provenance = corpus
    for seconds in (0.1, 0.25, 0.75, 1.5):
        pair = [case for case in manifest["cases"] if f"mix-gap-{seconds:g}s-" in case["caseId"]]
        assert len(pair) == 2
        assert pair[0]["audioPath"] == pair[1]["audioPath"]
        assert pair[0]["sourceArtifactSha256"] == pair[1]["sourceArtifactSha256"]
        assert {case["annotations"][0]["label"] for case in pair} == {"defect", "intentional"}
        for case in pair:
            annotation = case["annotations"][0]
            assert annotation["category"] == "mix_dropout"
            first, stop = [round(annotation[key] * 8000) for key in ("startSeconds", "endSeconds")]
            changed = pcm(Path(case["audioPath"]))[0]
            assert changed[:first * 4] == original[:first * 4]
            assert changed[stop * 4:] == original[stop * 4:]
            assert changed[first * 4:stop * 4] == b"\0" * ((stop - first) * 4)
            region = case["reviewedRanges"][0]
            assert annotation["startSeconds"] - region["startSeconds"] <= 0.050001
            if "intentPath" in case:
                snapshot = load_rhythm_intent(case["intentPath"], audio=case["audioPath"])
                assert snapshot.sections[0]["expectedRest"] is True
                assert snapshot.sections[0]["startSeconds"] == annotation["startSeconds"]
        record, = [item for item in provenance["transforms"] if item["caseId"] == pair[0]["caseId"]]
        assert record["semanticScope"].startswith("Known full-mix")


def test_identical_smooth_tempo_waveform_has_separate_declaration_condition(corpus):
    _, _, _, manifest, _ = corpus
    pair = [case for case in manifest["cases"] if "smooth-tempo-" in case["caseId"]]
    assert len(pair) == 2
    assert len({case["sourceArtifactSha256"] for case in pair}) == 1
    assert len({case["audioPath"] for case in pair}) == 1
    assert sum("intentPath" in case for case in pair) == 1
    assert all(case["annotations"][0]["label"] == "intentional" for case in pair)


def test_corpus_refuses_existing_output_and_leaves_source_bytes_unchanged(corpus):
    source, _, result, _, _ = corpus
    before = source.read_bytes()
    baseline = Path(result["manifestPath"]).parent / "baseline.wav"
    existing = baseline.read_bytes()
    with pytest.raises(FileExistsError):
        builder.build_corpus(source, baseline.parent, split="calibration", source_family="known-pulses")
    assert source.read_bytes() == before
    assert baseline.read_bytes() == existing


def test_silent_unsupported_source_does_not_invent_peaks_or_changed_audio_labels(tmp_path):
    source = tmp_path / "silence.wav"
    write_source(source, seconds=24, silence=True)
    result = builder.build_corpus(source, tmp_path / "silent-corpus", split="holdout", source_family="silent-control",
                                  strengths_ms=(50,), durations_seconds=(4,), kinds=("burst",))
    manifest = json.loads(Path(result["manifestPath"]).read_text())
    provenance = json.loads(Path(result["provenancePath"]).read_text())
    assert provenance["measuredSourcePulse"]["bpm"] is None
    assert all(annotation["label"] == "uncertain" for case in manifest["cases"] for annotation in case["annotations"])
    warp, = [item for item in provenance["transforms"] if item["kind"] == "controlled_time_warp"]
    assert warp["measuredSourceOnsetMapping"] == []
    assert warp["flankContext"]["eightStablePulsesOnEachFlank"] is False


@pytest.mark.parametrize("pattern", ["alternating", "burst", "smooth_tempo"])
@pytest.mark.parametrize("strength", [50, 90, 130, 200])
def test_time_maps_remain_monotone_bounded_and_return_to_original_phase(pattern, strength):
    source, target = builder.timing_mapping(10, 4, strength, kind=pattern, observed_period=0.2, observed_anchor=10.3)
    assert source[0] == target[0] == 10
    assert source[-1] == target[-1] == 14
    assert np.all(np.diff(source) > 0)
    assert np.all(np.diff(target) > 0)
    assert np.max(np.abs(source - target)) <= strength / 1000 + 1e-10


def test_nonidentity_support_retains_interpolation_neighbors_and_separate_identity_segments():
    source = np.arange(10, 16.01, 0.5)
    target = source.copy()
    target[5:8] += [-0.13, 0.13, -0.13]
    assert builder.time_map_support(source, target) == [(12.0, 14.0)]
    # The first/last displaced knots are not the support boundaries.
    assert target[4] == source[4] == 12
    assert target[8] == source[8] == 14
    assert builder.time_map_support(source, source) == []
    target = source.copy()
    target[2] += 0.1
    target[8] -= 0.1
    assert builder.time_map_support(source, target) == [(10.5, 11.5), (13.5, 14.5)]


@pytest.mark.parametrize("pattern", ["alternating", "smooth_tempo"])
def test_full_support_maps_keep_the_authored_annotation_region(pattern):
    source, target = builder.timing_mapping(10, 6, 130, kind=pattern, observed_period=0.5)
    assert builder.time_map_support(source, target) == [(10, 16)]


@pytest.mark.parametrize("source,target", [
    ([0, 1, 2], [0, float("nan"), 2]),
    ([0, 1, 2], [0, 2, 1]),
    ([0, 1, 2], [0.1, 1, 2]),
    ([[0, 1, 2]], [[0, 1, 2]]),
])
def test_support_refuses_unbounded_invalid_or_nonidentity_boundary_maps(source, target):
    with pytest.raises(ValueError, match="finite monotone"):
        builder.time_map_support(source, target)


def _historical_broad_truth(result, manifest, provenance):
    """Make a v1 historical record without consulting any detector prediction."""
    provenance["version"] = "controlled-rhythm-corpus-v1"
    by_case = {item["caseId"]: item for item in provenance["transforms"]}
    for case in manifest["cases"]:
        transform = by_case[case["caseId"]]
        if transform["kind"] in ("controlled_time_warp", "deliberate_smooth_time_warp"):
            a, b = transform["regionSeconds"]
            case["annotations"][0].update(startSeconds=a, endSeconds=b)
            case["reviewedRanges"][0].update(startSeconds=a, endSeconds=b)
            for key in ("authoredRegionSeconds", "timeMapSupportSeconds", "truthRegionBasis"):
                transform.pop(key)
        # Merely retaining this path must never cause a prediction read.
        case["reportPath"] = str(Path(result["manifestPath"]).parent / "unavailable-predictions.json")
    Path(result["manifestPath"]).write_text(json.dumps(manifest), encoding="utf-8")
    Path(result["provenancePath"]).write_text(json.dumps(provenance), encoding="utf-8")


def test_explicit_revision_reuses_verified_audio_and_preserves_all_previous_artifacts(corpus, monkeypatch):
    source, _, result, manifest, provenance = corpus
    _historical_broad_truth(result, manifest, provenance)
    directory = Path(result["manifestPath"]).parent
    before = {path.name: (path.stat().st_size, sha256_file(path)) for path in directory.iterdir()}
    source_before = sha256_file(source)
    old_manifest_hash, old_provenance_hash = sha256_file(Path(result["manifestPath"])), sha256_file(Path(result["provenancePath"]))
    def forbidden(*args, **kwargs):
        pytest.fail("Truth refinement must not analyze, regenerate, or warp audio")
    for name in ("analyze_percussion", "_atomic_wav", "warp_pcm"):
        monkeypatch.setattr(builder, name, forbidden)
    new_manifest, new_provenance = directory / "corpus-support-v2.json", directory / "provenance-support-v2.json"
    revision = builder.refine_existing_corpus(result["manifestPath"], result["provenancePath"], new_manifest, new_provenance)
    assert revision["caseCount"] == 15
    assert revision["changedCaseCount"] == 1
    assert revision["newAudioFiles"] == 0
    assert revision["verifiedReusedArtifactCount"] == 11
    for name, (size, digest) in before.items():
        assert (directory / name).stat().st_size == size
        assert sha256_file(directory / name) == digest
    assert sha256_file(source) == source_before
    assert {path.name for path in directory.iterdir()} - set(before) == {new_manifest.name, new_provenance.name}
    revised = json.loads(new_manifest.read_text())
    validate_manifest(revised)
    previous_by_case = {case["caseId"]: case for case in manifest["cases"]}
    for case in revised["cases"]:
        previous = previous_by_case[case["caseId"]]
        assert case["sourceArtifactSha256"] == previous["sourceArtifactSha256"]
        assert case["audioPath"] == previous["audioPath"]
        assert case["reportPath"] == previous["reportPath"]
        assert [item["label"] for item in case["annotations"]] == [item["label"] for item in previous["annotations"]]
        if "-burst-" not in case["caseId"]:
            assert case["reviewedRanges"] == previous["reviewedRanges"]
        for annotation in case["annotations"]:
            assert annotation["evidence"]["sourceRef"] == str(new_provenance) + "#" + case["caseId"]
    record = json.loads(new_provenance.read_text())["revision"]
    assert record["previousManifest"]["sha256"] == old_manifest_hash
    assert record["previousProvenance"]["sha256"] == old_provenance_hash
    assert record["sourceArtifactSha256"] == source_before
    assert record["detectorOutputsUsed"] is False
    assert record["boundarySource"] == "authored_input_output_time_knots_only"
    assert all(item["verification"] == "file_exists_bytes_sha256" for item in record["reusedArtifacts"])
    preserved_revision = (new_manifest.read_bytes(), new_provenance.read_bytes())
    with pytest.raises(FileExistsError):
        builder.refine_existing_corpus(result["manifestPath"], result["provenancePath"], new_manifest, new_provenance)
    assert preserved_revision == (new_manifest.read_bytes(), new_provenance.read_bytes())


@pytest.mark.parametrize("artifact,corruption", [("audio", "size"), ("audio", "sha256"), ("source", "sha256")])
def test_revision_refuses_corrupted_reused_artifacts_without_publishing(corpus, artifact, corruption):
    source, _, result, manifest, _ = corpus
    path = source if artifact == "source" else Path(manifest["cases"][1]["audioPath"])
    original = path.read_bytes()
    path.write_bytes(original[:-1] if corruption == "size" else bytes([original[0] ^ 1]) + original[1:])
    directory = Path(result["manifestPath"]).parent
    new_manifest, new_provenance = directory / "refined.json", directory / "refined-provenance.json"
    with pytest.raises(ValueError, match="size changed" if corruption == "size" else "SHA-256 changed"):
        builder.refine_existing_corpus(result["manifestPath"], result["provenancePath"], new_manifest, new_provenance)
    assert not new_manifest.exists()
    assert not new_provenance.exists()


def test_explicit_clip_is_bounded_and_missing_tail_is_rejected(tmp_path):
    source = tmp_path / "long.wav"
    original = write_source(source, seconds=65)
    data, metadata = builder._read_clip(source, 5, 60)
    assert metadata["durationSeconds"] == 60
    assert data == original[5 * 8000 * 4:]
    with pytest.raises(ValueError, match="at least 24"):
        builder._read_clip(source, 50, 60)


def test_optional_project_binding_requires_a_verified_completed_candidate(tmp_path):
    source = tmp_path / "source.wav"
    write_source(source)
    store = ProjectStore.initialize(tmp_path / "song", title="fixture", lyrics="[Instrumental]",
                                    style_prompt="test", target_duration_seconds=30)
    destination = store.root / "source.wav"
    destination.write_bytes(source.read_bytes())
    artifact = {"artifactId": "artifact-source", "path": "source.wav", "bytes": destination.stat().st_size,
                "sha256": sha256_file(destination)}
    with store.transaction() as project:
        project["artifacts"].append(artifact)
        project["candidates"].append({"candidateId": "candidate-source", "artifactId": artifact["artifactId"], "status": "ready"})
    bound = builder._project_source(store.root, source, sha256_file(source))
    assert bound["candidateId"] == "candidate-source"
    destination.write_bytes(b"changed")
    with pytest.raises(ValueError, match="size changed"):
        builder._project_source(store.root, source, sha256_file(source))


def test_atomic_wav_publication_never_replaces_an_existing_artifact(tmp_path):
    path = tmp_path / "output.wav"
    metadata = {"channels": 1, "sampleWidthBytes": 2, "sampleRate": 8000}
    builder._atomic_wav(path, b"\0\0" * 100, metadata)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        builder._atomic_wav(path, b"\1\0" * 100, metadata)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
