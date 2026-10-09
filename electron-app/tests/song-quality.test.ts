import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { handleArtifactRequest } from "../main/audio.ts";
import { loadSong, qualityFromStatus, versionFilePath } from "../main/songs.ts";
import { qualityLabel } from "../renderer/quality.ts";
import type { RhythmQuality } from "../shared.ts";

test("in-progress raw quality metadata is normalized before renderer access", () => {
  const report = qualityFromStatus({ complete: false, preferred: false, status: "unknown", summary: "자동 검사를 진행하고 있어요.", attempt: 1, maxAttempts: 4 });
  assert.equal(report?.lyrics.status, "unknown");
  assert.deepEqual(report?.retryReasons, []);
  assert.deepEqual(report?.audio, {});
  assert.equal(report?.processing, null);
  assert.equal(qualityLabel(report)?.label, "자동 검사 중");
  assert.equal(report?.rhythm, undefined);
});

test("incomplete rhythm metadata stays unknown and unsafe numeric observations are discarded", () => {
  const rhythm = { requestedBpm: 120, estimatedBpm: Number.NaN, status: "invalid", confidence: Infinity,
    meter: { status: "projected", timeSignature: "4/4" }, segments: [{ startSeconds: 5, endSeconds: 4 }],
    frameSeries: { columns: ["timeSeconds", "rmsDbfs"], points: [[0, -15], [1, Infinity], [2], [3, null]], intervalSeconds: -1 },
    findings: [{ check: "rhythm_tempo", severity: "failure", message: "Invalid rhythm failure." }] };
  const report = qualityFromStatus({ rhythm: rhythm as unknown as RhythmQuality })!;
  assert.equal(report.rhythm?.status, "unknown");
  assert.equal(report.rhythm?.requestedBpm, 120);
  assert.equal(report.rhythm?.estimatedBpm, null);
  assert.equal(report.rhythm?.confidence, 0);
  assert.equal(report.rhythm?.meter.status, "unknown");
  assert.deepEqual(report.rhythm?.segments, []);
  assert.deepEqual(report.rhythm?.frameSeries.points, [[0, -15], [3, null]]);
  assert.deepEqual(report.rhythm?.findings, []);
});

test("malformed diagnostics cannot establish backing continuity and nonfinite evidence or ranges remain unusable", () => {
  const diagnostics = { version: "rhythm-diagnostics-v1", status: "observed", durationSeconds: 30,
    checks: {
      beatTiming: { status: "observed", source: { kind: "percussive_estimate", method: "HPSS", reliability: 0.7 }, observed: { timingDeviationP95Milliseconds: Infinity } },
      backingContinuity: { status: "observed", source: { kind: "percussive_estimate", method: "HPSS", reliability: 0.7 }, observed: {} },
    }, events: [
      { check: "backing_dropout_suspected", category: "backing_dropout", severity: "failure", evidenceStatus: "suspected", confidence: 0.7, startSeconds: 1, endSeconds: 2 },
      { check: "beat_timing_instability_suspected", category: "beat_timing", severity: "warning", evidenceStatus: "suspected", confidence: 0.7, startSeconds: 1, endSeconds: Infinity },
      { check: "backing_dropout_suspected", category: "beat_timing", severity: "warning", evidenceStatus: "suspected", confidence: 0.7, startSeconds: 1, endSeconds: 2 },
      { check: "beat_timing_instability_suspected", category: "beat_timing", severity: "warning", evidenceStatus: "suspected", confidence: 0.7, startSeconds: 3, endSeconds: 5,
        observed: { timingDeviationP95Milliseconds: 55, nested: { invalidValue: Number.NaN } }, threshold: {}, retryEligible: true },
    ], findings: [], limitations: ["Only an estimate.", false] };
  const report = qualityFromStatus({ rhythm: { diagnostics } as unknown as RhythmQuality })!.rhythm!.diagnostics!;
  assert.equal(report.status, "unknown");
  assert.equal(report.checks.beatTiming.status, "observed");
  assert.equal(report.checks.beatTiming.observed.timingDeviationP95Milliseconds, null);
  assert.equal(report.checks.backingContinuity.status, "unknown");
  assert.equal(report.checks.backingContinuity.source, null);
  assert.equal(report.events.length, 1);
  assert.equal(report.events[0]?.retryEligible, false);
  assert.deepEqual(report.events[0]?.observed.nested, { invalidValue: null });
  assert.deepEqual(report.limitations, ["Only an estimate."]);
  const unsupported = qualityFromStatus({ rhythm: { diagnostics: { version: "future", status: "observed" } } as unknown as RhythmQuality })!;
  assert.equal(unsupported.rhythm!.diagnostics, null);
});

test("v2 diagnostics and bounded omitted counts survive the main-to-renderer boundary", () => {
  const diagnostics = { version: "rhythm-diagnostics-v2", status: "needs_review", durationSeconds: 30,
    checks: {
      beatTiming: { status: "needs_review", source: { kind: "percussive_estimate", method: "HPSS", reliability: 0.7 }, observed: {} },
      backingContinuity: { status: "unknown", source: null, observed: {} },
    }, events: [], findings: [], limitations: [], eventsTruncated: true,
    totalEventCount: 70, omittedEventCount: 6, omittedWarningCount: 6 };
  const report = qualityFromStatus({ rhythm: { diagnostics } as unknown as RhythmQuality })!.rhythm!.diagnostics!;
  assert.equal(report.version, "rhythm-diagnostics-v2");
  assert.equal(report.checks.beatTiming.status, "needs_review");
  assert.equal(report.omittedWarningCount, 6);
  assert.equal(report.eventsTruncated, true);
  const malformed = qualityFromStatus({ rhythm: { diagnostics: { ...diagnostics,
    totalEventCount: Infinity, omittedEventCount: -1, omittedWarningCount: 1.5,
  } } as unknown as RhythmQuality })!.rhythm!.diagnostics!;
  assert.equal(malformed.totalEventCount, undefined);
  assert.equal(malformed.omittedEventCount, undefined);
  assert.equal(malformed.omittedWarningCount, undefined);
});

test("v3 PCM observations and declared-rest evidence survive the renderer boundary", () => {
  const event = { check: "mix_dropout_suspected", category: "mix_dropout", severity: "info", confidence: 1,
    startSeconds: 2.7, endSeconds: 3.2, evidenceStatus: "consistent_with_declared_silence",
    observed: { intent: "unknown", intentAssessment: "consistent_with_declared_silence",
      gapDurationSeconds: 0.5, relativeDepthDb: 200, declaredRestRangeSeconds: [2.7, 3.2] },
    threshold: {}, retryEligible: true };
  const diagnostics = { version: "rhythm-diagnostics-v3", status: "observed", durationSeconds: 8,
    checks: {
      beatTiming: { status: "observed", source: { kind: "percussive_estimate", method: "HPSS", reliability: 0.7 }, observed: {} },
      backingContinuity: { status: "observed", source: { kind: "separated_accompaniment", method: "cached stem", reliability: 0.8 }, observed: {} },
      mixContinuity: { status: "observed", source: { kind: "full_mix_pcm", method: "all-channel PCM", reliability: 1 }, observed: { measuredDropoutCount: 1 } },
    }, events: [event], findings: [event], limitations: [], totalEventCount: 1,
    omittedEventCount: 0, omittedWarningCount: 0, eventsTruncated: false };
  const result = qualityFromStatus({ rhythm: { diagnostics } as unknown as RhythmQuality })!.rhythm!.diagnostics!;
  assert.equal(result.version, "rhythm-diagnostics-v3");
  assert.equal(result.checks.mixContinuity?.source?.kind, "full_mix_pcm");
  assert.equal(result.checks.mixContinuity?.status, "observed");
  assert.equal(result.events.length, 1);
  assert.equal(result.events[0]?.evidenceStatus, "consistent_with_declared_silence");
  assert.equal(result.events[0]?.observed.intent, "unknown");
  assert.deepEqual(result.events[0]?.observed.declaredRestRangeSeconds, [2.7, 3.2]);
  assert.equal(result.events[0]?.retryEligible, false);
  assert.deepEqual(result.findings, result.events);
});

test("v3 requires valid PCM evidence for observed status and preserves warning outcomes", () => {
  const checks = {
    beatTiming: { status: "observed", source: { kind: "percussive_estimate", method: "HPSS", reliability: 0.7 }, observed: {} },
    backingContinuity: { status: "observed", source: { kind: "separated_accompaniment", method: "cached stem", reliability: 0.8 }, observed: {} },
  };
  const report = (extra: unknown) => qualityFromStatus({ rhythm: { diagnostics: {
    version: "rhythm-diagnostics-v3", status: "observed", durationSeconds: 8,
    checks: { ...checks, ...extra as object }, events: [], findings: [], limitations: [],
  } } as unknown as RhythmQuality })!.rhythm!.diagnostics!;
  const missing = report({});
  assert.equal(missing.status, "unknown");
  assert.equal(missing.checks.mixContinuity?.status, "unknown");
  const invalid = report({ mixContinuity: {
    status: "observed", source: { kind: "separated_accompaniment", method: "cached stem", reliability: 1 }, observed: {},
  } });
  assert.equal(invalid.status, "unknown");
  assert.equal(invalid.checks.mixContinuity?.source, null);
  const warning = report({ mixContinuity: {
    status: "needs_review", source: { kind: "full_mix_pcm", method: "all-channel PCM", reliability: 1 }, observed: {},
  } });
  assert.equal(warning.status, "needs_review");
  for (const version of ["rhythm-diagnostics-v1", "rhythm-diagnostics-v2"]) {
    const legacy = qualityFromStatus({ rhythm: { diagnostics: {
      version, status: "observed", durationSeconds: 8, checks, events: [], findings: [], limitations: [],
    } } as unknown as RhythmQuality })!.rhythm!.diagnostics!;
    assert.equal(legacy.status, "observed");
    assert.equal(legacy.checks.mixContinuity, undefined);
  }
});

test("song loading exposes a separate recommendation and streams the finished artifact while preserving raw audio and review", async () => {
  const folder = await mkdtemp(path.join(tmpdir(), "music-quality-song-"));
  try {
    const original = path.join(folder, "original.wav");
    const finished = path.join(folder, "finished.wav");
    await writeFile(original, "preserved raw");
    await writeFile(finished, "separate finished artifact");
    const review = { status: "unreviewed", rating: null, notes: [], updatedAt: null };
    const quality = { status: "passed", summary: "자동 검사 통과", groupId: "batch-slot", attempt: 2, maxAttempts: 4,
      originalSeed: 11, preferred: false, score: 98, retryReasons: [], audio: { automaticStatus: "passed" },
      lyrics: { status: "pass", orderedCoverage: 1 }, processing: null };
    const candidate = { candidateId: "raw", selected: false, seed: 12, taskType: "text2music", parentCandidateId: null, editRange: null,
      durationSeconds: 10, artifactValid: true, artifactValidation: "verified", humanReview: review, findings: [],
      stylePrompt: "piano", lyrics: "가사", model: "local", bpm: null, keyScale: null, repaintStrength: null,
      instruction: null, feedbackId: null, createdAt: null, path: original, quality, recommended: false };
    const raw = { projectId: "project", title: "곡", path: folder, createdAt: null, updatedAt: null,
      inputs: { stylePrompt: "piano", lyrics: "가사", durationSeconds: 10, structure: null, bpm: null, keyScale: null, timeSignature: null },
      selectedCandidateId: null, recommendedCandidateId: "finished", canUndoSelection: false, generationActive: false, feedback: [], jobs: [], exports: [],
      candidates: [candidate, { ...candidate, candidateId: "finished", parentCandidateId: "raw", taskType: "postprocess", path: finished,
        quality: { ...quality, preferred: true, processing: { sourceCandidateId: "raw", gain: 0.95 } }, recommended: true }] };
    const state = await loadSong(folder, async <T>() => raw as unknown as T);
    assert.equal(state.status, "ready");
    assert.equal(state.song?.recommendedVersionId, "finished");
    assert.equal(state.song?.finalVersionId, null);
    const winner = state.song!.versions.find((version) => version.id === "finished")!;
    assert.equal(winner.review.status, "unreviewed");
    assert.equal(winner.isFinal, false);
    assert.equal(winner.recommended, true);
    assert.equal(winner.quality?.attempt, 2);
    assert.equal(versionFilePath("raw"), original);
    assert.equal(versionFilePath("finished"), finished);
    assert.equal(await (await handleArtifactRequest(new Request(winner.audioUrl!))).text(), "separate finished artifact");
    assert.equal(await (await handleArtifactRequest(new Request(state.song!.versions[0]!.audioUrl!))).text(), "preserved raw");
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});

test("v4 keeps repeated-hit timing evidence and treats missing or invalid evidence as unknown", () => {
  const shift = { check: "repeated_hit_timing_shift_suspected", category: "beat_timing", severity: "warning", confidence: 0.8,
    startSeconds: 2, endSeconds: 5, evidenceStatus: "intent_unknown",
    observed: { mode: "repetition_displacement", peakDeviationMilliseconds: 90, deviantHitCount: 9,
      measuredOnsetTimesSeconds: [2.1, 2.6], expectedTimesSeconds: [2.05, 2.66], intent: "unknown" },
    threshold: { minimumDeviationMilliseconds: 15 }, retryEligible: true };
  const checks = {
    beatTiming: { status: "observed", source: { kind: "percussive_estimate", method: "HPSS", reliability: 0.7 }, observed: {} },
    backingContinuity: { status: "observed", source: { kind: "separated_accompaniment", method: "cached stem", reliability: 0.8 }, observed: {} },
    mixContinuity: { status: "observed", source: { kind: "full_mix_pcm", method: "all-channel PCM", reliability: 1 }, observed: {} },
  };
  const report = (hitTiming: unknown, events: unknown[] = []) => qualityFromStatus({ rhythm: { diagnostics: {
    version: "rhythm-diagnostics-v4", status: "observed", durationSeconds: 8,
    checks: { ...checks, ...(hitTiming === undefined ? {} : { hitTiming }) }, events, findings: events, limitations: [],
  } } as unknown as RhythmQuality })!.rhythm!.diagnostics!;
  const source = { kind: "full_mix_pcm", method: "hits linked to their own repetitions", reliability: 0.8 };
  const warned = report({ status: "needs_review", source, observed: { supportedFraction: 0.9 } }, [shift]);
  assert.equal(warned.version, "rhythm-diagnostics-v4");
  assert.equal(warned.status, "needs_review");
  assert.equal(warned.checks.hitTiming?.status, "needs_review");
  assert.equal(warned.events[0]?.check, "repeated_hit_timing_shift_suspected");
  assert.equal(warned.events[0]?.retryEligible, false);
  assert.deepEqual(warned.events[0]?.observed.measuredOnsetTimesSeconds, [2.1, 2.6]);
  assert.deepEqual(warned.events[0]?.observed.expectedTimesSeconds, [2.05, 2.66]);
  assert.equal(report(undefined).status, "unknown");
  assert.equal(report(undefined).checks.hitTiming?.status, "unknown");
  const wrongSource = report({ status: "observed", source: { kind: "percussive_estimate", method: "HPSS", reliability: 1 }, observed: {} });
  assert.equal(wrongSource.checks.hitTiming?.source, null);
  assert.equal(wrongSource.status, "unknown");
  // A hit-timing event is not accepted under another category, and v3 reports gain no such check.
  assert.equal(report({ status: "observed", source, observed: {} }, [{ ...shift, category: "mix_dropout" }]).events.length, 0);
  const v3 = qualityFromStatus({ rhythm: { diagnostics: { version: "rhythm-diagnostics-v3", status: "observed", durationSeconds: 8,
    checks, events: [], findings: [], limitations: [] } } as unknown as RhythmQuality })!.rhythm!.diagnostics!;
  assert.equal(v3.checks.hitTiming, undefined);
  assert.equal(v3.status, "observed");
});
