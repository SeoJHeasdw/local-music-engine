import assert from "node:assert/strict";
import { test } from "node:test";
import type { ActiveTask, AutomaticQuality, RhythmDiagnostics, RhythmQuality, Song, TaskOutcome, Version } from "../shared.ts";
import { findingCopy, versionSourceParent, versionTree } from "../renderer/format.ts";
import { audioQualityText, isAutomaticAttempt, lyricQualityText, processingText, qualityLabel, rhythmDiagnosticChecks, rhythmMeterText, rhythmQualityText, TaskListeningFocus } from "../renderer/quality.ts";

const quality = (change: Partial<AutomaticQuality> = {}): AutomaticQuality => ({
  status: "passed", summary: "자동 검사 통과", groupId: "batch-slot", attempt: 1, maxAttempts: 4,
  originalSeed: 10, preferred: false, score: 98, retryReasons: [],
  audio: { automaticStatus: "passed" }, lyrics: { status: "pass", orderedCoverage: 1 }, processing: null, ...change,
});

function version(id: string, change: Partial<Version> = {}): Version {
  return { id, kind: "full", parentId: null, isFinal: false, recommended: false,
    review: { status: "unreviewed", rating: null, notes: [], updatedAt: null }, quality: quality(), ...change } as Version;
}

test("two requested winners remain two numbered roots and raw retries are retained as history", () => {
  const a1 = version("a1");
  const a2 = version("a2", { quality: quality({ attempt: 2 }) });
  const winnerA = version("A", { parentId: "a2", recommended: true,
    quality: quality({ attempt: 2, preferred: true, processing: { sourceCandidateId: "a2", gain: 0.9 } }) });
  const b1 = version("b1", { quality: quality({ groupId: "batch-slot-b", originalSeed: 11 }) });
  const winnerB = version("B", { parentId: "b1",
    quality: quality({ groupId: "batch-slot-b", originalSeed: 11, preferred: true, processing: { sourceCandidateId: "b1", gain: 1 } }) });
  const song = { versions: [a1, a2, winnerA, b1, winnerB], finalVersionId: null, recommendedVersionId: "A" } as Song;
  const tree = versionTree(song);
  const ready = tree.roots.filter((node) => !isAutomaticAttempt(node.version));
  assert.deepEqual(ready.map((node) => [node.version.id, node.name, node.depth]), [["A", "버전 1", 0], ["B", "버전 2", 0]]);
  assert.deepEqual(tree.flat.filter((node) => isAutomaticAttempt(node.version)).map((node) => node.version.id), ["a1", "a2", "b1"]);
  assert.match(tree.byId.get("a1")!.name, /버전 1.*시도 1/);
  assert.match(tree.byId.get("b1")!.name, /버전 2.*시도 1/);
  assert.equal(versionSourceParent(song, winnerA)?.id, "a2");
  assert.equal(winnerA.review.status, "unreviewed");
  assert.equal(winnerA.isFinal, false);
  assert.equal(song.finalVersionId, null);
});

test("a finished repaint follows its musical source rather than adding a raw-attempt nesting level", () => {
  const original = version("original", { quality: null });
  const raw = version("raw", { kind: "edit", parentId: "original" });
  const finished = version("finished", { kind: "edit", parentId: "raw",
    quality: quality({ preferred: true, processing: { sourceCandidateId: "raw" } }) });
  const song = { versions: [original, raw, finished] } as Song;
  const result = versionTree(song).byId.get("finished")!;
  assert.equal(result.name, "버전 1 › 수정 1");
  assert.equal(result.depth, 1);
  assert.equal(versionSourceParent(song, finished)?.id, "original");
});

test("a cover and its cleaned result keep the trusted source as their comparison parent", () => {
  const original = version("original", { quality: null });
  const raw = version("raw-cover", { kind: "cover", parentId: "original", coverStrength: 0.7 });
  const finished = version("finished-cover", { kind: "cover", parentId: "raw-cover", coverStrength: 0.7,
    quality: quality({ preferred: true, processing: { sourceCandidateId: "raw-cover" } }) });
  const song = { versions: [original, raw, finished] } as Song;
  assert.equal(versionTree(song).byId.get("finished-cover")!.name, "버전 1 › 커버 1");
  assert.equal(versionSourceParent(song, finished)?.id, "original");
  assert.equal(original.review.status, "unreviewed");
});

test("unknown analysis is visibly unknown and conservative processing never claims repaired clipping", () => {
  const unknown = quality({ status: "unknown", audio: {}, lyrics: { status: "unknown", orderedCoverage: 1 } });
  assert.match(qualityLabel(unknown)!.label, /어려움/);
  assert.doesNotMatch(qualityLabel(unknown)!.label, /통과/);
  assert.match(lyricQualityText(unknown), /판단하지/);
  assert.match(audioQualityText(unknown), /확정하지/);
  const text = processingText({ gain: 0.8, endFadeSeconds: 0.005, channelDcOffsetsRemoved: [0.01] })!;
  assert.match(text, /음량을 낮췄어요/);
  assert.match(text, /원래 음원도 남아/);
  assert.doesNotMatch(text, /깨짐.*고쳤|클리핑.*수리/);
});

const rhythm = (change: Partial<RhythmQuality> = {}): RhythmQuality => ({
  status: "observed", requestedBpm: 120, estimatedBpm: 119.6, confidence: 0.8, segments: [], findings: [],
  meter: { status: "projected", timeSignature: "4/4", quarterNotesPerBar: 4, caveat: "Projected only." },
  frameSeries: { columns: [], points: [], analysisHopSeconds: 0.01, intervalSeconds: 1 }, ...change,
});

test("rhythm observations distinguish estimates and requested tempo without claiming repaired or verified beats", () => {
  const report = quality({ rhythm: rhythm() });
  assert.match(rhythmQualityText(report)!, /요청 120 BPM.*자동 추정 119\.6 BPM/);
  assert.match(rhythmMeterText(report)!, /4\/4.*계산/);
  assert.match(rhythmMeterText(report)!, /실제 마디 시작과 강박은 확인되지/);
  assert.doesNotMatch(rhythmQualityText(report)!, /통과|보정|고쳤|완벽/);
  assert.equal(version("observed", { quality: report }).review.status, "unreviewed");
});

test("uncertain rhythm and warning states remain listenable observations", () => {
  const unknown = quality({ rhythm: rhythm({ status: "unknown", estimatedBpm: 90 }) });
  assert.match(rhythmQualityText(unknown)!, /확정하지/);
  assert.doesNotMatch(rhythmQualityText(unknown)!, /자동 추정 90|통과/);
  const review = quality({ rhythm: rhythm({ status: "needs_review" }) });
  assert.match(rhythmQualityText(review)!, /표시한 구간을 들어/);
  assert.doesNotMatch(rhythmQualityText(review)!, /보정|고쳤/);
  const absentEstimate = quality({ rhythm: rhythm({ estimatedBpm: null }) });
  assert.match(rhythmQualityText(absentEstimate)!, /확정하지/);
  const audioChangeWithoutTempo = quality({ rhythm: rhythm({ status: "needs_review", estimatedBpm: null }) });
  assert.match(rhythmQualityText(audioChangeWithoutTempo)!, /템포 추정 어려움.*소리 변화.*들어/);
  const finding = findingCopy({ findingId: "rhythm-warning", check: "tempo_mismatch", severity: "warning",
    message: "An observed pulse differs.", observed: { estimatedBpm: 100, requestedBpm: 120 }, threshold: {}, startSeconds: 10, endSeconds: 20 });
  assert.equal(finding.label, "요청 템포와 차이");
  assert.match(finding.message, /들어서 확인/);
  assert.match(finding.value!, /자동 추정 100.*요청 120/);
});

test("older quality reports omit the optional rhythm check", () => {
  assert.equal(rhythmQualityText(quality()), null);
  assert.equal(rhythmMeterText(quality()), null);
  assert.equal(rhythmQualityText(quality({ rhythm: null })), null);
  assert.deepEqual(rhythmDiagnosticChecks(quality()), []);
  assert.deepEqual(rhythmDiagnosticChecks(quality({ rhythm: rhythm() })).map((check) => check.label), ["박자 불안정 판단 어려움", "반주 끊김 판단 어려움"]);
});

const diagnostics = (change: Partial<RhythmDiagnostics> = {}): RhythmDiagnostics => ({
  version: "rhythm-diagnostics-v1", status: "needs_review", durationSeconds: 30,
  checks: {
    beatTiming: { status: "needs_review", reason: "Local rhythm estimate varies.", source: { kind: "percussive_estimate", method: "HPSS", reliability: 0.6 }, observed: { timingDeviationP95Milliseconds: 58, observedPulseCount: 46 } },
    backingContinuity: { status: "needs_review", reason: "Backing dip while vocals continue.", source: { kind: "separated_accompaniment", method: "Demucs", reliability: 0.9 },
      vocalSource: { kind: "separated_vocal", method: "Demucs", reliability: 0.9 }, observed: {} },
  },
  events: [
    { check: "beat_timing_instability_suspected", category: "beat_timing", severity: "warning", confidence: 0.6,
      startSeconds: 3, endSeconds: 8, evidenceStatus: "suspected", message: "Timing may be unstable.", observed: { timingDeviationP95Milliseconds: 58 }, threshold: {}, retryEligible: false },
    { check: "backing_dropout_suspected", category: "backing_dropout", severity: "warning", confidence: 0.7,
      startSeconds: 12.1, endSeconds: 12.9, evidenceStatus: "suspected", message: "Backing may have dropped.", observed: { relativeDepthDb: 18.5 }, threshold: {}, retryEligible: false },
  ], findings: [], limitations: ["These are observations, not confirmed musical errors."], ...change,
});

test("timing instability and accompaniment dropout stay separate suspected findings with their evidence and source", () => {
  const report = quality({ rhythm: rhythm({ diagnostics: diagnostics() }) });
  const checks = rhythmDiagnosticChecks(report);
  assert.deepEqual(checks.map((check) => check.label), ["박자 불안정 의심", "반주 끊김 의심"]);
  assert.match(checks[0]!.evidence!, /3–8초.*58\.0 ms/);
  assert.match(checks[0]!.method!, /추정한 타격 성분.*HPSS.*추정/);
  assert.match(checks[1]!.evidence!, /12\.1–12\.9초.*18\.5 dB/);
  assert.match(checks[1]!.method!, /분리된 반주.*Demucs.*분리 오차/);
  assert.match(checks[1]!.method!, /분리된 목소리/);
  assert.doesNotMatch(checks.map((check) => `${check.label} ${check.text}`).join(" "), /통과|오류 확정|보정했/);
  assert.equal(version("diagnosed", { quality: report }).review.status, "unreviewed");
  for (const event of report.rhythm!.diagnostics!.events) {
    const copy = findingCopy({ findingId: event.check, ...event });
    assert.match(copy.label, /의심/);
    assert.match(copy.message, /들어서 확인/);
  }
});

test("observed HPSS timing never establishes accompaniment continuity without backing evidence", () => {
  const basis = diagnostics();
  const report = quality({ rhythm: rhythm({ diagnostics: diagnostics({ status: "unknown", events: [], checks: {
    beatTiming: { ...basis.checks.beatTiming, status: "observed" },
    backingContinuity: { status: "unknown", reason: "No reliable backing stem.", source: null, observed: {} },
  } }) }) });
  const checks = rhythmDiagnosticChecks(report);
  assert.equal(checks[0]!.label, "박자 간격 관찰");
  assert.equal(checks[1]!.label, "반주 끊김 판단 어려움");
  assert.match(checks[1]!.text, /근거가 부족해/);
  assert.equal(checks[1]!.evidence, null);
  assert.equal(checks[1]!.method, null);
});

test("possible arrangement breaks and percussive gaps do not become suspected backing errors", () => {
  const basis = diagnostics();
  const gap = { ...basis.events[0]!, check: "percussive_gap_observed" as const, category: "percussion_gap" as const,
    severity: "info" as const, evidenceStatus: "observed" as const, observed: { gapSeconds: 0.8 } };
  const arrangement = { ...basis.events[1]!, check: "possible_arrangement_break" as const, category: "arrangement_break" as const,
    severity: "info" as const, evidenceStatus: "intent_unknown" as const };
  const report = quality({ rhythm: rhythm({ diagnostics: diagnostics({ status: "observed", events: [gap, arrangement] }) }) });
  assert.deepEqual(rhythmDiagnosticChecks(report).map((check) => check.label), ["타격 소리 쉼 관찰", "편곡 쉼 가능성"]);
  const copy = findingCopy({ findingId: "arrangement", ...arrangement });
  assert.match(copy.label, /의도 확인 필요/);
  assert.doesNotMatch(copy.label, /끊김 의심/);
  const intentionalTiming = { ...basis.events[0]!, severity: "info" as const, evidenceStatus: "intent_unknown" as const };
  const intentional = quality({ rhythm: rhythm({ diagnostics: diagnostics({ status: "observed", events: [intentionalTiming] }) }) });
  assert.equal(rhythmDiagnosticChecks(intentional)[0]?.label, "박자 변화 · 의도 확인 필요");
  assert.equal(findingCopy({ findingId: "intentional", ...intentionalTiming }).label, "박자 변화 · 의도 확인 필요");
});

test("v2 warnings remain visible and omitted warning counts do not claim a complete display", () => {
  const report = quality({ rhythm: rhythm({ diagnostics: diagnostics({
    version: "rhythm-diagnostics-v2", eventsTruncated: true, totalEventCount: 70,
    omittedEventCount: 6, omittedWarningCount: 6,
  }) }) });
  const checks = rhythmDiagnosticChecks(report);
  assert.equal(checks[0]!.label, "박자 불안정 의심");
  assert.equal(checks[1]!.label, "반주 끊김 의심");
  assert.match(checks[0]!.text, /6개 관측 구간.*생략/);
  assert.match(checks[1]!.text, /생략된 경고도 6개/);
  assert.doesNotMatch(checks.map((check) => check.text).join(" "), /추가 구간 검사를 하지 않은/);
});

test("v3 adds independent full-mix warnings without claiming all music quality is verified", () => {
  const basis = diagnostics();
  const event = { check: "mix_dropout_suspected" as const, category: "mix_dropout" as const,
    severity: "warning" as const, confidence: 1, startSeconds: 2.7, endSeconds: 3.2,
    evidenceStatus: "intent_unknown" as const, message: "Abrupt near-zero PCM.",
    observed: { gapDurationSeconds: 0.5, relativeDepthDb: 200, intent: "unknown" },
    threshold: {}, retryEligible: false as const };
  const report = quality({ rhythm: rhythm({ diagnostics: diagnostics({
    version: "rhythm-diagnostics-v3", events: [event],
    checks: { ...basis.checks,
      mixContinuity: { status: "needs_review", reason: "Abrupt near-zero PCM.",
        source: { kind: "full_mix_pcm", method: "all-channel PCM", reliability: 1 }, observed: {} },
    },
  }) }) });
  const checks = rhythmDiagnosticChecks(report);
  assert.equal(checks.length, 3);
  assert.equal(checks[2]!.label, "전체 신호 끊김 의심");
  assert.match(checks[2]!.evidence!, /2\.7–3\.2초.*200\.0 dB/);
  assert.match(checks[2]!.method!, /원본 파일의 전체 신호.*all-channel PCM/);
  assert.match(checks[2]!.text, /의도된 편집인지 자동으로 확정하지/);
  assert.doesNotMatch(checks[2]!.text, /오류 확정|보정했|완벽|검사 통과/);
  assert.equal(version("v3", { quality: report }).review.status, "unreviewed");
});

test("declared full rest is shown as compatibility while retaining unknown musical intent", () => {
  const basis = diagnostics();
  const report = quality({ rhythm: rhythm({ diagnostics: diagnostics({
    version: "rhythm-diagnostics-v3", status: "unknown",
    events: [{ check: "mix_dropout_suspected", category: "mix_dropout", severity: "info", confidence: 1,
      startSeconds: 2, endSeconds: 2.5, evidenceStatus: "consistent_with_declared_silence", message: "Declared rest.",
      observed: { intentAssessment: "consistent_with_declared_silence", intent: "unknown" }, threshold: {}, retryEligible: false }],
    checks: { ...basis.checks,
      mixContinuity: { status: "observed", reason: "Declared local rest.",
        source: { kind: "full_mix_pcm", method: "all-channel PCM", reliability: 1 }, observed: {} },
    },
  }) }) });
  const check = rhythmDiagnosticChecks(report)[2]!;
  assert.equal(check.label, "선언한 쉼과 일치");
  assert.match(check.text, /구간 전체가 선언한 쉼 계획/);
  assert.match(check.text, /음악 품질과 사람 청취 승인을 확정한 결과는 아니/);
  assert.equal(report.rhythm!.diagnostics!.events[0]!.observed.intent, "unknown");
  for (const version of ["rhythm-diagnostics-v1", "rhythm-diagnostics-v2"] as const) {
    const oldReport = quality({ rhythm: rhythm({ diagnostics: diagnostics({ version }) }) });
    assert.equal(rhythmDiagnosticChecks(oldReport).length, 2);
  }
});

test("full-mix continuity alone describes a scoped check and omitted warnings remain visible", () => {
  const basis = diagnostics();
  const report = quality({ rhythm: rhythm({ diagnostics: diagnostics({
    version: "rhythm-diagnostics-v3", status: "needs_review", events: [], eventsTruncated: true,
    totalEventCount: 70, omittedEventCount: 6, omittedWarningCount: 6,
    checks: { ...basis.checks,
      mixContinuity: { status: "observed", reason: "No abrupt zero gap found.",
        source: { kind: "full_mix_pcm", method: "all-channel PCM", reliability: 1 }, observed: {} },
    },
  }) }) });
  const check = rhythmDiagnosticChecks(report)[2]!;
  assert.match(check.text, /모든 음악 오류를 확인하는 검사는 아니/);
  assert.match(check.text, /생략된 경고도 6개/);
  assert.doesNotMatch(check.text, /모든 오류.*없|품질.*통과|완벽/);
});

test("omitted full-mix warnings retain the needs-review check when no warning event is displayed", () => {
  const basis = diagnostics();
  const report = quality({ rhythm: rhythm({ diagnostics: diagnostics({
    version: "rhythm-diagnostics-v3", events: [], eventsTruncated: true,
    totalEventCount: 65, omittedEventCount: 1, omittedWarningCount: 1,
    checks: { ...basis.checks,
      mixContinuity: { status: "needs_review", reason: "Warning omitted behind earlier warnings.",
        source: { kind: "full_mix_pcm", method: "all-channel PCM", reliability: 1 }, observed: { measuredDropoutCount: 1 } },
    },
  }) }) });
  const check = rhythmDiagnosticChecks(report)[2]!;
  assert.equal(check.label, "전체 신호 연속성 확인 필요");
  assert.match(check.text, /생략된 구간도 전체 검사 상태에 포함/);
  assert.match(check.text, /생략된 경고도 1개/);
  assert.doesNotMatch(check.label, /연속성 관찰|쉼과 일치/);
});

const task = { songId: "song", startedAt: 10 } as ActiveTask;
const outcome = { songId: "song", kind: "generate", ok: true, cancelled: false,
  newVersionIds: ["A", "B"], recommendedVersionId: "B" } as TaskOutcome;

test("recommendations focus automatically only while the listener has not changed song or version", () => {
  const unchanged = new TaskListeningFocus();
  unchanged.track(task, 5);
  unchanged.track({ ...task, progress: 0.8 }, 5);
  unchanged.track(null, 5);
  assert.equal(unchanged.complete(outcome, "song", 5), "B");
  const switched = new TaskListeningFocus();
  switched.track(task, 5);
  switched.track({ ...task, progress: 0.8 }, 7);
  assert.equal(switched.complete(outcome, "song", 7), null);
  const otherSong = new TaskListeningFocus();
  otherSong.track(task, 5);
  assert.equal(otherSong.complete(outcome, "another", 5), null);
  const reused = new TaskListeningFocus();
  reused.track(task, 5);
  assert.equal(reused.complete({ ...outcome, recommendedVersionId: "old" }, "song", 5), null);
});

const hitTimingCheck = (change: Partial<RhythmDiagnostics["checks"]["beatTiming"]> = {}) => ({
  status: "needs_review" as const, reason: "Hits compared with their own repetitions.",
  source: { kind: "full_mix_pcm" as const, method: "hits linked to their own repetitions", reliability: 0.8 },
  observed: { supportedFraction: 0.82, linkedHitCount: 300 }, ...change,
});

test("v4 reports repeated-hit timing in its own row without merging it into the pulse-interval check", () => {
  const basis = diagnostics();
  const shift = { check: "repeated_hit_timing_shift_suspected" as const, category: "beat_timing" as const,
    severity: "warning" as const, confidence: 0.8, startSeconds: 27.3, endSeconds: 32.9,
    evidenceStatus: "intent_unknown" as const, message: "Several consecutive hits arrive early or late.",
    observed: { mode: "repetition_displacement", peakDeviationMilliseconds: 131.4, deviantHitCount: 22, intent: "unknown" },
    threshold: {}, retryEligible: false as const };
  const report = quality({ rhythm: rhythm({ diagnostics: diagnostics({
    version: "rhythm-diagnostics-v4", events: [shift],
    checks: { ...basis.checks, beatTiming: { ...basis.checks.beatTiming, status: "observed" }, hitTiming: hitTimingCheck() },
  }) }) });
  const checks = rhythmDiagnosticChecks(report);
  const row = checks.at(-1)!;
  assert.equal(row.label, "타격 시각 흔들림 의심");
  assert.match(row.evidence!, /확인할 구간 1곳 · 27\.3–32\.9초.*131 ms.*어긋난 타격 22회.*곡의 82%에서 판단/);
  assert.match(row.method!, /원본 파일의 전체 신호/);
  assert.match(row.text, /의도한 변주일 수 있으니.*직접 들어서 확인/);
  assert.doesNotMatch(row.text, /오류 확정|보정했|완벽|검사 통과/);
  // The older pulse-interval row neither counts nor describes the new finding.
  assert.equal(checks[0]!.label, "박자 간격 관찰");
  assert.doesNotMatch(checks[0]!.evidence ?? "", /27\.3/);
  const finding = findingCopy({ findingId: "hit-shift", check: shift.check, severity: "warning", message: shift.message,
    observed: shift.observed, threshold: {}, startSeconds: 27.3, endSeconds: 32.9 });
  assert.equal(finding.label, "타격 시각 흔들림 의심");
  assert.match(finding.value!, /131 ms/);
  assert.equal(version("v4", { quality: report }).review.status, "unreviewed");
});

test("gradual tempo change, unsupported audio and clean repetition each get an honest hit-timing label", () => {
  const basis = diagnostics();
  const gradual = { check: "smooth_timing_change_observed" as const, category: "beat_timing" as const,
    severity: "info" as const, confidence: 0.5, startSeconds: 28.1, endSeconds: 32,
    evidenceStatus: "observed" as const, message: "Hit timing departs gradually and returns.",
    observed: { mode: "tempo_drift", peakExpectedDisplacementMilliseconds: 195.4, intent: "unknown" }, threshold: {}, retryEligible: false as const };
  const row = (hitTiming: RhythmDiagnostics["checks"]["beatTiming"], events: RhythmDiagnostics["events"] = []) =>
    rhythmDiagnosticChecks(quality({ rhythm: rhythm({ diagnostics: diagnostics({
      version: "rhythm-diagnostics-v4", status: "observed", events, checks: { ...basis.checks, hitTiming } }) }) })).at(-1)!;
  const smooth = row(hitTimingCheck({ status: "observed" }), [gradual]);
  assert.equal(smooth.label, "완만한 템포 변화 관찰");
  assert.match(smooth.text, /급격한 흔들림은 아니/);
  assert.match(smooth.evidence!, /28\.1–32초.*195 ms/);
  const unknown = row(hitTimingCheck({ status: "unknown", observed: {} }));
  assert.equal(unknown.label, "타격 시각 판단 어려움");
  assert.equal(unknown.evidence, null);
  assert.match(unknown.text, /반복되는 근거가 부족해.*판단하지 않았/);
  const clean = row(hitTimingCheck({ status: "observed" }));
  assert.equal(clean.label, "타격 시각 관찰");
  assert.match(clean.text, /반복이 없는 구간은 판단하지 않았/);
  assert.doesNotMatch(clean.text, /통과|정상|완벽/);
  assert.equal(findingCopy({ findingId: "gradual", check: gradual.check, severity: "info", message: gradual.message,
    observed: gradual.observed, threshold: {}, startSeconds: 28.1, endSeconds: 32 }).label, "완만한 템포 변화 관찰");
  // Earlier diagnosis versions never show the row.
  const older = rhythmDiagnosticChecks(quality({ rhythm: rhythm({ diagnostics: diagnostics({ version: "rhythm-diagnostics-v3" }) }) }));
  assert.ok(older.every((check) => !check.label.includes("타격 시각")));
});
