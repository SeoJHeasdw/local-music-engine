import type { ActiveTask, AutomaticQuality, Finding, RhythmDiagnosticCheck, RhythmDiagnosticEvent, TaskOutcome, Version, VersionLoop } from "../shared.ts";

export function isAutomaticAttempt(version: Version): boolean {
  return version.quality?.preferred === false && !version.isFinal && !version.recommended;
}

// Every arranged song changes level and tone between sections, and a repeating pulse can be
// read at half or double speed. Those are measurements, not something a listener is sent to check.
const structureObservations = new Set(["loudness_shift", "spectral_shift", "tempo_ambiguity"]);

/** Warnings that name something to check, in song order; ones without a time come last. */
export function listeningPoints(findings: Finding[]): Finding[] {
  return findings.filter((finding) => finding.severity !== "info" && !structureObservations.has(finding.check))
    .sort((a, b) => (a.startSeconds ?? Infinity) - (b.startSeconds ?? Infinity));
}

// A missing section already has its own row with the place to play; it is not said twice.
const placedReasons = new Set(["written_section_missing"]);
const lyricPlaces = new Set(["lyric_lines_missing", "lyric_lines_unclear"]);

/** What the automatic check still holds against this version, one sentence each. */
export function remainingProblems(quality: AutomaticQuality, findings: Finding[] = []): string[] {
  const placed = findings.some((finding) => lyricPlaces.has(finding.check));
  const reasons = [...new Set(quality.retryReasons.filter((reason) => !(placed && placedReasons.has(reason))).map(qualityRetryReason))];
  if (!reasons.length && !placed && quality.lyrics.status === "warning") reasons.push(lyricQualityText(quality));
  if (quality.finishingError) reasons.push("재생본 정리를 마치지 못해 정리 전 음원이에요. 위의 '이어서 만들기'를 누르면 정리만 다시 해요.");
  return reasons;
}

// Counts only what the checks panel can show the listener: a problem in words or a place
// to play. A version whose only notes are section changes has nothing to check.
export function checkCount(version: Pick<Version, "quality" | "findings">): number {
  return (version.quality ? remainingProblems(version.quality, version.findings).length : 0) + listeningPoints(version.findings).length;
}

/** For an edit: what is known about the audio outside the edited stretch. */
export function editSpliceText(version: Pick<Version, "splice" | "editRange" | "quality">): string | null {
  const splice = version.splice;
  if (!splice || !version.editRange) return null;
  const changed = splice.changedRange;
  if (!splice.applied || !changed) return "엔진이 만든 그대로예요. 고른 구간 밖의 소리도 조금 달라졌을 수 있으니 원본과 비교해 들어 보세요.";
  const seconds = (value: number) => Number(value.toFixed(2)).toString();
  const span = `${seconds(changed.startSeconds)}–${seconds(changed.endSeconds)}초`;
  const repair = version.quality?.repair;
  if (repair?.accepted) {
    const lines = `"${repair.firstLine}"${repair.lineCount > 1 ? ` 등 ${repair.lineCount}줄` : ""}`;
    return `처음 만든 음원에서 들리지 않던 가사(${lines})를 넣으려고 ${span}만 다시 만들었고, 다시 받아써서 불린 것을 확인했어요. 그 밖은 처음 만든 소리와 샘플 하나까지 같아요. 위의 '비교 버전'으로 고치기 전을 들을 수 있어요.`;
  }
  return `${span}만 새로 만든 소리예요. 그 밖은 고치기 전 버전과 샘플 하나까지 같아요.`;
}

export function qualityLabel(version: Pick<Version, "quality" | "findings">): { label: string; tone: string } | null {
  const quality = version.quality;
  if (!quality) return null;
  if (quality.complete === false) return { label: "자동 검사 중", tone: "muted" };
  const count = checkCount(version);
  if (count) return { label: `확인할 것 ${count}개`, tone: "warn" };
  if (quality.status === "unknown") return { label: "일부 자동 확인 어려움", tone: "muted" };
  return { label: "자동 검사 통과", tone: "ok" };
}

export function lyricQualityText(quality: AutomaticQuality): string {
  switch (quality.lyrics.status) {
    case "pass": return "인식한 가사가 작성한 가사의 흐름과 맞아요.";
    case "warning": return "인식한 가사에 차이가 있어요. 누락되거나 달라진 부분을 들어 보세요.";
    case "not_applicable": return "연주곡이라 가사 검사를 하지 않았어요.";
    default: return "가사를 신뢰할 만큼 인식하지 못해 자동으로 판단하지 않았어요.";
  }
}

const reasons: Record<string, string> = {
  low_ordered_lyric_coverage: "작성한 가사가 많이 빠지거나 순서가 달라요.",
  high_korean_cer: "인식한 한글 가사에 차이가 많아요.",
  high_english_wer: "인식한 영어 가사에 차이가 많아요.",
  large_korean_transcript_error: "인식한 한글 가사에 차이가 많아요.",
  large_english_transcript_error: "인식한 영어 가사에 차이가 많아요.",
  written_chorus_occurrence_missing: "작성한 후렴이 일부 빠진 것으로 보여요.",
  written_section_missing: "작성한 구절 하나가 통째로 들리지 않아요.",
  sustained_clipping: "소리가 지속적으로 찌그러질 가능성이 있어요.",
  duration_mismatch: "요청한 곡 길이와 크게 달라요.",
  digital_silence: "음원 전체에 소리가 거의 없어요.",
};

export function qualityRetryReason(reason: string): string {
  return reasons[reason] ?? reasons[reason.split(":").at(-1) ?? ""] ?? (/[가-힣]/.test(reason) ? reason : "자동 검사에서 뚜렷한 문제를 찾아 다시 만들었어요.");
}

export function audioQualityText(quality: AutomaticQuality): string {
  switch (quality.audio.automaticStatus) {
    case "passed": return "소리 깨짐·무음·곡 길이 검사에서 뚜렷한 문제를 찾지 못했어요.";
    case "needs_review": return "소리에서 확인할 부분이 있어요. 자동 생성 이력과 비교해 들어 보세요.";
    case "retry_recommended": return "소리나 곡 길이에 뚜렷한 문제가 남아 있어요.";
    default: return "소리 자동 검사를 끝내지 못해 상태를 확정하지 않았어요.";
  }
}

export function rhythmQualityText(quality: AutomaticQuality): string | null {
  const rhythm = quality.rhythm;
  if (!rhythm) return null;
  const requested = typeof rhythm.requestedBpm === "number" && Number.isFinite(rhythm.requestedBpm) && rhythm.requestedBpm > 0
    ? `요청 ${formatBpm(rhythm.requestedBpm)} BPM` : "요청 템포 없음";
  const estimated = typeof rhythm.estimatedBpm === "number" && Number.isFinite(rhythm.estimatedBpm) && rhythm.estimatedBpm > 0
    ? rhythm.estimatedBpm : null;
  if (rhythm.status === "unknown" || (rhythm.status !== "needs_review" && estimated === null)) return `${requested}. 리듬을 신뢰할 만큼 추정하지 못해 상태를 확정하지 않았어요.`;
  const observation = rhythm.status === "needs_review"
    ? "템포나 구간별 소리 변화에서 확인할 부분이 있어요. 표시한 구간을 들어 보세요."
    : "박자 간격과 구간별 변화를 관찰했어요.";
  const estimate = estimated === null ? "템포 추정 어려움" : `자동 추정 ${formatBpm(estimated)} BPM`;
  return `${requested} · ${estimate}. ${observation}`;
}

function formatBpm(value: number): string {
  return Number(value.toFixed(1)).toString();
}

export function rhythmMeterText(quality: AutomaticQuality): string | null {
  const rhythm = quality.rhythm;
  if (!rhythm) return null;
  if (rhythm.meter?.status !== "projected" || !rhythm.meter.timeSignature) return "마디 위치와 강박은 자동으로 확인하지 못했어요.";
  return `요청한 ${rhythm.meter.timeSignature} 기준으로 마디 길이를 템포에서 계산했어요. 실제 마디 시작과 강박은 확인되지 않았어요.`;
}

export type RhythmDiagnosticCopy = { label: string; text: string; evidence: string | null; method: string | null };

export function rhythmDiagnosticChecks(quality: AutomaticQuality): RhythmDiagnosticCopy[] {
  if (!quality.rhythm) return [];
  const diagnostics = quality.rhythm.diagnostics;
  const absent = !diagnostics || !["rhythm-diagnostics-v1", "rhythm-diagnostics-v2", "rhythm-diagnostics-v3", "rhythm-diagnostics-v4"].includes(diagnostics.version);
  const beat = absent ? null : diagnostics.checks.beatTiming;
  const backing = absent ? null : diagnostics.checks.backingContinuity;
  const events = absent ? [] : diagnostics.events;
  // Repetition-referenced hit timing has its own row below; it is a different measurement.
  const beatEvents = events.filter((event) => event.check === "beat_timing_instability_suspected" || event.check === "percussive_gap_observed");
  const backingEvents = events.filter((event) => event.category === "backing_dropout" || event.category === "arrangement_break");
  const unstable = beat?.status !== "unknown" && beatEvents.some((event) => event.check === "beat_timing_instability_suspected" && event.severity === "warning");
  const timingChange = beat?.status !== "unknown" && beatEvents.some((event) => event.check === "beat_timing_instability_suspected" && event.severity === "info");
  const gap = beat?.status !== "unknown" && beatEvents.some((event) => event.check === "percussive_gap_observed");
  const dropout = backing?.status !== "unknown" && backingEvents.some((event) => event.check === "backing_dropout_suspected");
  const arrangement = backing?.status !== "unknown" && backingEvents.some((event) => event.check === "possible_arrangement_break");
  const beatUnknown = !beat || beat.status === "unknown";
  const backingUnknown = !backing || backing.status === "unknown";
  const omitted = diagnostics?.omittedEventCount ?? 0;
  const omittedWarnings = diagnostics?.omittedWarningCount ?? 0;
  const limitNotice = !absent && omitted > 0
    ? ` 전체 보고서는 중요한 경고부터 표시하며, ${Math.round(omitted)}개 관측 구간이 표시 한도로 생략됐어요.${omittedWarnings > 0 ? ` 생략된 경고도 ${Math.round(omittedWarnings)}개 있어요.` : ""}` : "";
  const copies = [
    {
      label: beatUnknown ? "박자 불안정 판단 어려움" : unstable ? "박자 불안정 의심" : timingChange ? "박자 변화 · 의도 확인 필요" : gap ? "타격 소리 쉼 관찰" : beat.status === "needs_review" ? "박자 간격 확인 필요" : "박자 간격 관찰",
      text: beatUnknown ? absent ? "추가 구간 검사를 하지 않은 결과예요." : "반복 박자 간격을 신뢰할 만큼 관찰하지 못해 자동으로 판단하지 않았어요."
        : unstable ? "추정한 박자 간격이 불안정한 구간이 있어요. 연주 방식과 분리 오차를 포함해 직접 들어서 확인해 주세요."
        : timingChange ? "추정한 박자 간격이 변하는 부분이에요. 요청한 템포와 그루브 변화에 맞는지 들어 보세요."
        : gap ? "타격 성분의 반복 소리가 비는 구간을 관찰했어요. 의도한 드럼 쉼인지 들어 보세요."
        : beat.status === "needs_review" ? "박자 간격에서 확인할 부분이 있어요. 직접 들어서 확인해 주세요."
        : "분리한 리듬 성분에서 박자 간격을 관찰했어요. 들리는 박자의 오류 여부는 직접 확인해 주세요.",
      evidence: beatUnknown ? null : diagnosticEvidence(beat!, unstable || timingChange ? beatEvents.filter((event) => event.category === "beat_timing") : beatEvents),
      method: diagnosticMethod(beat),
    },
    {
      label: backingUnknown ? "반주 끊김 판단 어려움" : dropout ? "반주 끊김 의심" : arrangement ? "편곡 쉼 가능성" : backing.status === "needs_review" ? "반주 연속성 확인 필요" : "반주 연속성 관찰",
      text: backingUnknown ? absent ? "추가 구간 검사를 하지 않은 결과예요." : "반주와 노래의 연속성을 비교할 근거가 부족해 자동으로 판단하지 않았어요."
        : dropout ? "분리된 반주가 주변보다 약해지는 구간이 있어요. 분리 오차와 의도한 쉼을 포함해 직접 들어서 확인해 주세요."
        : arrangement ? "반주가 줄어드는 구간을 관찰했어요. 의도한 편곡 쉼인지 자동으로 확정하지 못했어요."
        : backing.status === "needs_review" ? "반주의 연속성에서 확인할 부분이 있어요. 직접 들어서 확인해 주세요."
        : "분리된 반주에서 소리의 연속성을 관찰했어요. 빠진 악기와 의도한 쉼은 직접 확인해 주세요.",
      evidence: backingUnknown ? null : diagnosticEvidence(backing!, dropout ? backingEvents.filter((event) => event.category === "backing_dropout") : backingEvents),
      method: diagnosticMethod(backing),
    },
  ];
  const mix = absent ? undefined : diagnostics.checks.mixContinuity;
  if (mix) {
    const mixEvents = events.filter((event) => event.category === "mix_dropout");
    const warning = mixEvents.some((event) => event.severity === "warning");
    const planned = mixEvents.some((event) => event.observed.intentAssessment === "consistent_with_declared_silence");
    copies.push({ label: mix.status === "unknown" ? "전체 신호 끊김 판단 어려움" : warning ? "전체 신호 끊김 의심" : mix.status === "needs_review" ? "전체 신호 연속성 확인 필요" : planned ? "선언한 쉼과 일치" : "전체 신호 연속성 관찰",
      text: mix.status === "unknown" ? "전체 신호의 연속성을 확인할 근거가 부족해요."
        : warning ? "곡 중간에서 소리가 급격히 거의 0이 됐다 돌아오는 부분을 관찰했어요. 의도된 편집인지 자동으로 확정하지 못했어요."
        : mix.status === "needs_review" ? "전체 신호의 연속성에서 확인할 부분이 있어요. 표시 한도로 생략된 구간도 전체 검사 상태에 포함돼요."
        : planned ? "신호가 비는 구간 전체가 선언한 쉼 계획에 들어 있어요. 음악 품질과 사람 청취 승인을 확정한 결과는 아니에요."
        : "파일의 전체 신호에서 급격한 디지털 무음 구간을 검사했어요. 모든 음악 오류를 확인하는 검사는 아니에요.",
      evidence: mix.status === "unknown" ? null : diagnosticEvidence(mix, mixEvents), method: diagnosticMethod(mix),
    });
  }
  const hits = absent ? undefined : diagnostics.checks.hitTiming;
  if (hits) {
    const shifted = events.filter((event) => event.check === "repeated_hit_timing_shift_suspected" && event.severity === "warning");
    const gradual = events.filter((event) => event.check === "smooth_timing_change_observed");
    copies.push({
      label: hits.status === "unknown" ? "타격 시각 판단 어려움" : shifted.length ? "타격 시각 흔들림 의심"
        : hits.status === "needs_review" ? "타격 시각 확인 필요" : gradual.length ? "완만한 템포 변화 관찰" : "타격 시각 관찰",
      text: hits.status === "unknown" ? "곡 안에서 같은 타격이 반복되는 근거가 부족해 타격 시각을 판단하지 않았어요."
        : shifted.length ? "연속된 여러 타격이 곡 안의 다른 반복보다 이르거나 늦게 나와요. 의도한 변주일 수 있으니 이 구간을 직접 들어서 확인해 주세요."
        : hits.status === "needs_review" ? "타격 시각에서 확인할 부분이 있어요. 표시 한도로 생략된 구간도 전체 검사 상태에 포함돼요."
        : gradual.length ? "타격이 반복 기준에서 서서히 벗어났다 돌아오는 구간이에요. 급격한 흔들림은 아니에요. 의도한 템포 변화인지 들어 보세요."
        : "같은 타격을 곡 안의 다른 반복과 비교했고 급격한 어긋남은 찾지 못했어요. 반복이 없는 구간은 판단하지 않았어요.",
      evidence: hits.status === "unknown" ? null : hitTimingEvidence(hits, shifted.length ? shifted : gradual), method: diagnosticMethod(hits),
    });
  }
  return copies.map((check) => ({ ...check, text: check.text + limitNotice }));
}

function hitTimingEvidence(check: RhythmDiagnosticCheck, events: RhythmDiagnosticEvent[]): string | null {
  const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
  const details: string[] = [];
  const first = events[0];
  if (first) {
    details.push(`확인할 구간 ${events.length}곳 · ${Number(first.startSeconds.toFixed(2))}–${Number(first.endSeconds.toFixed(2))}초`);
    if (finite(first.observed.peakDeviationMilliseconds)) details.push(`가장 크게 어긋난 타격 ${first.observed.peakDeviationMilliseconds.toFixed(0)} ms`);
    if (finite(first.observed.deviantHitCount)) details.push(`어긋난 타격 ${Math.round(first.observed.deviantHitCount)}회`);
    if (finite(first.observed.peakExpectedDisplacementMilliseconds)) details.push(`가장 크게 벗어난 지점 ${first.observed.peakExpectedDisplacementMilliseconds.toFixed(0)} ms`);
  }
  if (finite(check.observed.supportedFraction)) details.push(`곡의 ${Math.round(Math.max(0, Math.min(1, check.observed.supportedFraction)) * 100)}%에서 판단`);
  return details.length ? details.join(" · ") : null;
}

function diagnosticMethod(check: RhythmDiagnosticCheck | null): string | null {
  if (!check?.source) return null;
  const labels: Record<NonNullable<RhythmDiagnosticCheck["source"]>["kind"], string> = {
    isolated_percussion: "분리된 타격 성분", percussive_estimate: "전체 음원에서 추정한 타격 성분", synthetic_percussion: "검사용 타격 성분",
    separated_accompaniment: "분리된 반주", separated_instrumental: "분리된 악기", synthetic_accompaniment: "검사용 반주",
    separated_vocal: "분리된 목소리", synthetic_vocal: "검사용 목소리",
    full_mix_pcm: "원본 파일의 전체 신호",
  };
  const sources = [check.source, check.vocalSource].filter((source) => source != null);
  const methods = sources.map((source) => `${labels[source.kind]}${source.method ? ` · ${source.method.slice(0, 120)}` : ""}`).join(" / ");
  return `관찰 자료: ${methods}. 분리 오차와 연주 방식의 영향을 받는 추정이에요.`;
}

function diagnosticEvidence(check: RhythmDiagnosticCheck, events: RhythmDiagnosticEvent[]): string | null {
  const values = { ...check.observed, ...events[0]?.observed };
  const details: string[] = [];
  if (events.length) details.push(`확인할 구간 ${events.length}곳 · ${Number(events[0]!.startSeconds.toFixed(2))}–${Number(events[0]!.endSeconds.toFixed(2))}초`);
  if (typeof values.timingDeviationP95Milliseconds === "number" && Number.isFinite(values.timingDeviationP95Milliseconds)) details.push(`추정 박자 간격 편차(95백분위) ${values.timingDeviationP95Milliseconds.toFixed(1)} ms`);
  if (typeof values.relativeDepthDb === "number" && Number.isFinite(values.relativeDepthDb)) details.push(`주변보다 ${values.relativeDepthDb.toFixed(1)} dB 작음`);
  if (typeof values.observedPulseCount === "number" && Number.isFinite(values.observedPulseCount)) details.push(`관찰한 반복 소리 ${Math.max(0, Math.round(values.observedPulseCount))}회`);
  const context = values.contextEvidence && typeof values.contextEvidence === "object"
    ? values.contextEvidence as Record<string, unknown> : null;
  if (context?.crossCueStatus === "source_allocation_conflict") details.push("분리 추정과 원본의 타격 성분이 서로 맞지 않음");
  const repeated = context?.repeatedEnvelope && typeof context.repeatedEnvelope === "object"
    ? context.repeatedEnvelope as Record<string, unknown> : null;
  if (repeated?.status === "recurring_shape_observed") details.push("곡 안에 비슷한 음량 형태가 반복됨 · 의도는 미확정");
  return details.length ? details.join(" · ") : null;
}

export class TaskListeningFocus {
  private started: { songId: string; startedAt: number; revision: number } | null = null;

  track(task: ActiveTask | null, revision: number): void {
    if (task && (this.started?.startedAt !== task.startedAt || this.started.songId !== task.songId)) {
      this.started = { songId: task.songId, startedAt: task.startedAt, revision };
    }
  }

  complete(outcome: TaskOutcome, songId: string | undefined, revision: number): string | null {
    const started = this.started;
    this.started = null;
    if (!started || started.songId !== outcome.songId || outcome.songId !== songId || started.revision !== revision || !outcome.ok || outcome.cancelled) return null;
    if (outcome.recommendedVersionId && outcome.newVersionIds.includes(outcome.recommendedVersionId)) return outcome.recommendedVersionId;
    return outcome.kind === "repaint" ? outcome.newVersionIds[0] ?? null : null;
  }
}

export function processingText(processing: Record<string, unknown> | null): string | null {
  if (!processing) return null;
  const values = typeof processing.processing === "object" && processing.processing !== null
    ? processing.processing as Record<string, unknown> : processing;
  const changes: string[] = [];
  if (Array.isArray(values.channelDcOffsetsRemoved) && values.channelDcOffsetsRemoved.some((offset) => typeof offset === "number" && offset !== 0)) changes.push("소리의 치우침을 정리했어요");
  if (typeof values.gain === "number" && values.gain < 1) changes.push("너무 큰 음량을 낮췄어요");
  if (Number(values.startFadeSeconds ?? 0) > 0 || Number(values.endFadeSeconds ?? 0) > 0) changes.push("시작·끝의 갑작스러운 소리를 줄였어요");
  return changes.length ? `${changes.join(". ")}. 원래 음원도 남아 있어요.` : "소리를 더 크게 만들거나 곡의 쉼을 잘라내지 않았어요. 원래 음원도 남아 있어요.";
}

// format.ts imports this module, so the loop length is written here: "1분 4초", "48초".
function loopLength(seconds: number): string {
  const rounded = Math.round(seconds);
  const minutes = Math.floor(rounded / 60);
  return minutes ? `${minutes}분${rounded % 60 ? ` ${rounded % 60}초` : ""}` : `${rounded}초`;
}

// What each loop warning asks the listener to check. They are hints from measurement; the
// loop is still made and plays.
const LOOP_WARNINGS: Record<string, string> = {
  phase_ambiguous: "킥이 매 박 나오는 곡이라 마디 첫 박은 추정이에요. 이음새를 들어 확인해 주세요.",
  meter_assumed: "박자 정보가 없어 4/4로 보고 잘랐어요.",
  seam_level_differs: "이음새에서 소리 크기가 곡 안의 다른 마디 경계보다 크게 바뀌어요. 이음새를 들어 확인해 주세요.",
  mastering_gain_differs: "반복 구간 양 끝의 음량이 조금 달라요. 이음새를 들어 확인해 주세요.",
};

/** One line for a version row: how long the loop is, or that there is none. */
export function loopSummaryText(loop: VersionLoop | null | undefined): string | null {
  if (!loop) return null;
  return loop.status === "ready" ? `반복용 ${loopLength(loop.durationSeconds)} · ${loop.bars}마디` : "반복용 없음";
}

/** The loop's place in the song and anything to listen for, for the checks tab. */
export function loopReportLines(loop: VersionLoop | null | undefined): Array<{ text: string; tone: "ok" | "warning" }> {
  if (!loop) return [];
  if (loop.status !== "ready") return [{ text: loop.reason, tone: "warning" }];
  const lines: Array<{ text: string; tone: "ok" | "warning" }> = [{
    tone: "ok",
    text: `${loop.startSeconds.toFixed(2)}초부터 ${loop.endSeconds.toFixed(2)}초까지 ${loop.bars}마디(${loopLength(loop.durationSeconds)})를 마디 첫 박의 킥에 맞춰 잘랐어요. 실제 빠르기 ${Math.round(loop.bpm * 10) / 10} BPM.`,
  }];
  for (const warning of loop.warnings) lines.push({ text: LOOP_WARNINGS[warning] ?? warning, tone: "warning" });
  return lines;
}
