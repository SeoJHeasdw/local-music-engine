import type { ActiveTask, AutomaticQuality, TaskOutcome, Version } from "../shared.ts";

export function isAutomaticAttempt(version: Version): boolean {
  return version.quality?.preferred === false && !version.isFinal && !version.recommended;
}

export function qualityLabel(quality: AutomaticQuality | null | undefined): { label: string; tone: string } | null {
  if (!quality) return null;
  if (quality.complete === false) return { label: "자동 검사 중", tone: "muted" };
  if (quality.status === "passed") return { label: "자동 검사 통과", tone: "ok" };
  if (quality.status === "attention") return { label: "확인할 부분 있음", tone: "warn" };
  return { label: "일부 자동 확인 어려움", tone: "muted" };
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
