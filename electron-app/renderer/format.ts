import type { Finding, ReviewStatus, Song, Strength, TimeRange, Version } from "../shared.ts";
import { isAutomaticAttempt } from "./quality.ts";

export function clock(seconds: number | null | undefined, precise = false, decimalPlaces: 1 | 2 = 1): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return precise ? "0:00.0" : "0:00";
  const safe = Math.max(0, seconds);
  const scale = 10 ** decimalPlaces;
  const rounded = Math.round(safe * scale) / scale;
  if (precise) return `${Math.floor(rounded / 60)}:${(rounded % 60).toFixed(decimalPlaces).padStart(3 + decimalPlaces, "0")}`;
  const minutes = Math.floor(safe / 60);
  return `${minutes}:${String(Math.floor(safe % 60)).padStart(2, "0")}`;
}

export function rangeLabel(range: TimeRange, precise = false): string {
  return `${clock(range.startSeconds, precise)}–${clock(range.endSeconds, precise)}`;
}

export function lengthLabel(seconds: number | null | undefined): string {
  if (!seconds || !Number.isFinite(seconds)) return "길이 미상";
  const rounded = Math.round(seconds);
  const minutes = Math.floor(rounded / 60);
  const rest = rounded % 60;
  if (!minutes) return `${rest}초`;
  return rest ? `${minutes}분 ${rest}초` : `${minutes}분`;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const time = Date.parse(iso);
  if (!Number.isFinite(time)) return "";
  const seconds = Math.round((Date.now() - time) / 1000);
  if (seconds < 45) return "방금";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}일 전`;
  return new Date(time).toLocaleDateString("ko-KR", { month: "long", day: "numeric" });
}

export function elapsed(since: number): string {
  const seconds = Math.max(0, Math.round((Date.now() - since) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

export const reviewCopy: Record<ReviewStatus, { label: string; short: string }> = {
  unreviewed: { label: "아직 안 들음", short: "안 들음" },
  listened: { label: "들어 봤어요", short: "들었음" },
  approved: { label: "좋아요", short: "좋아요" },
  rejected: { label: "별로예요", short: "별로" },
};

export const strengthCopy: Record<Strength, { label: string; hint: string }> = {
  light: { label: "살짝", hint: "원래 소리를 최대한 살리고 조금만 바꿔요" },
  medium: { label: "보통", hint: "원래 흐름을 지키면서 눈에 띄게 바꿔요" },
  strong: { label: "많이", hint: "그 구간을 거의 새로 만들어요. 앞뒤와 어긋날 수 있어요" },
};

export function strengthFromValue(value: number | null): Strength | null {
  if (value === null) return null;
  if (value <= 0.3) return "light";
  if (value >= 0.7) return "strong";
  return "medium";
}

// Version names: full renders are numbered in creation order; an edit is named after
// its parent ("버전 2 › 수정 1") so the lineage is readable without ids.
export type VersionNode = { version: Version; name: string; short: string; depth: number; children: VersionNode[] };

export function versionTree(song: Song): { roots: VersionNode[]; flat: VersionNode[]; byId: Map<string, VersionNode> } {
  const byId = new Map<string, VersionNode>();
  const roots: VersionNode[] = [];
  let fullIndex = 0;
  const versions = new Map(song.versions.map((version) => [version.id, version]));
  for (const version of song.versions) {
    // An automatically finished render is a ready version. Its raw source remains
    // available in history, rather than turning every result into a nested edit.
    const source = version.parentId ? versions.get(version.parentId) : undefined;
    const parentId = version.quality?.processing ? source?.parentId : version.parentId;
    const parent = parentId ? byId.get(parentId) : undefined;
    if (!parent) {
      const automatic = isAutomaticAttempt(version);
      if (!automatic) fullIndex += 1;
      const name = automatic ? `자동 시도 ${version.quality?.attempt ?? 1} · ${version.quality?.processing ? "정리본" : "원본"}` : `버전 ${fullIndex}`;
      const node = { version, name, short: name, depth: 0, children: [] };
      byId.set(version.id, node);
      roots.push(node);
    } else {
      const automatic = isAutomaticAttempt(version);
      const index = parent.children.filter((child) => !isAutomaticAttempt(child.version) && (child.version.kind === "cover") === (version.kind === "cover")).length + 1;
      const short = automatic ? `자동 시도 ${version.quality?.attempt ?? 1} · ${version.quality?.processing ? "정리본" : "원본"}` : `${version.kind === "cover" ? "커버" : "수정"} ${index}`;
      const node = { version, name: `${parent.name} › ${short}`, short, depth: parent.depth + 1, children: [] };
      byId.set(version.id, node);
      parent.children.push(node);
    }
  }
  const flat: VersionNode[] = [];
  const walk = (nodes: VersionNode[]) => {
    for (const node of nodes) {
      flat.push(node);
      walk(node.children);
    }
  };
  walk(roots);
  const preferredNames = new Map(flat.filter((node) => !isAutomaticAttempt(node.version) && node.version.quality?.groupId)
    .map((node) => [node.version.quality!.groupId!, node.name]));
  for (const node of flat) {
    const group = node.version.quality?.groupId;
    if (isAutomaticAttempt(node.version) && group && preferredNames.has(group)) {
      node.name = `${preferredNames.get(group)} · 시도 ${node.version.quality?.attempt ?? 1} · ${node.version.quality?.processing ? "정리본" : "원본"}`;
      node.short = node.name;
    }
  }
  return { roots, flat, byId };
}

export function versionSourceParent(song: Song, version: Version): Version | undefined {
  const source = song.versions.find((item) => item.id === version.parentId);
  if (version.quality?.processing && source?.parentId) return song.versions.find((item) => item.id === source.parentId) ?? source;
  return source;
}

export function splitTags(caption: string): string[] {
  return caption
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

export function tagDiff(before: string, after: string): Array<{ op: "add" | "remove"; term: string }> {
  const old = splitTags(before);
  const next = splitTags(after);
  const oldKeys = new Set(old.map((tag) => tag.toLowerCase()));
  const newKeys = new Set(next.map((tag) => tag.toLowerCase()));
  return [
    ...old.filter((tag) => !newKeys.has(tag.toLowerCase())).map((term) => ({ op: "remove" as const, term })),
    ...next.filter((tag) => !oldKeys.has(tag.toLowerCase())).map((term) => ({ op: "add" as const, term })),
  ];
}

export function hasTag(caption: string, tag: string): boolean {
  return splitTags(caption).some((item) => item.toLowerCase() === tag.toLowerCase());
}

export function toggleTag(caption: string, tag: string): string {
  const tags = splitTags(caption);
  const exists = tags.some((item) => item.toLowerCase() === tag.toLowerCase());
  return (exists ? tags.filter((item) => item.toLowerCase() !== tag.toLowerCase()) : [...tags, tag]).join(", ");
}

export function findingCopy(finding: Finding): { label: string; message: string; value: string | null } {
  const warning = finding.severity !== "info";
  const observed = finding.observed as Record<string, number>;
  switch (finding.check) {
    case "wav_integrity":
      return {
        label: "파일 읽기",
        message: warning ? "WAV를 끝까지 읽지 못했어요." : "WAV 헤더와 소리 데이터를 끝까지 읽었어요.",
        value: observed.sampleRate ? `${(observed.sampleRate / 1000).toFixed(1)}kHz · ${observed.channels === 2 ? "스테레오" : `${observed.channels}채널`}` : null,
      };
    case "peak": {
      const db = observed.peak > 0 ? 20 * Math.log10(observed.peak) : -Infinity;
      return {
        label: "최대 음량",
        message: warning ? "소리가 최대치에 닿았어요. 찢어지는 소리가 없는지 들어 보세요." : "최대 음량이 경고 기준보다 낮아요. 소리의 왜곡 여부는 직접 들어 보세요.",
        value: Number.isFinite(db) ? `${db.toFixed(1)} dBFS` : null,
      };
    }
    case "silence":
      return {
        label: "무음",
        message: warning ? "소리가 거의 없는 부분이 많아요. 의도한 쉼인지 들어 보세요." : "전체 무음 비율이 경고 기준보다 낮아요.",
        value: Number.isFinite(observed.silentFraction) ? `전체의 ${(observed.silentFraction * 100).toFixed(1)}%` : null,
      };
    case "silence_region":
    case "peak_region":
      return {
        label: finding.check === "silence_region" ? "확인할 무음 구간" : "최대 음량에 가까운 구간",
        message: finding.check === "silence_region" ? "의도한 쉼인지 이 구간을 들어 보세요." : "찢어지는 소리가 있는지 이 구간을 들어 보세요.",
        value: Number.isFinite(observed.durationSeconds) ? `${observed.durationSeconds.toFixed(2)}초` : null,
      };
    case "duration":
      return {
        label: "길이",
        message: warning ? "요청한 길이와 0.5초 넘게 달라요." : "요청한 길이와 맞아요.",
        value: Number.isFinite(observed.actualSeconds)
          ? `${observed.actualSeconds.toFixed(2)}초 / 요청 ${Number(observed.requestedSeconds).toFixed(0)}초`
          : null,
      };
    case "tempo_mismatch":
    case "tempo_ambiguity":
      return {
        label: finding.check === "tempo_mismatch" ? "요청 템포와 차이" : "박자 해석 확인",
        message: finding.check === "tempo_mismatch"
          ? "반복 박자의 자동 추정값이 요청한 템포와 달라요. 박자 단위와 의도한 그루브를 들어서 확인해 주세요."
          : "반복 박자가 요청 템포와 다른 박자 단위로 추정됐어요. 듣는 박자 단위를 확인해 주세요.",
        value: Number.isFinite(observed.estimatedBpm) && Number.isFinite(observed.requestedBpm)
          ? `자동 추정 ${Number(observed.estimatedBpm.toFixed(1))} / 요청 ${observed.requestedBpm} BPM` : null,
      };
    case "tempo_drift":
      return {
        label: "구간별 템포 변화",
        message: warning
          ? "구간별 반복 박자 추정값이 달라요. 의도한 템포 변화나 리듬 분할일 수 있어 이 구간을 들어 보세요."
          : "구간별 반복 박자 추정값이 달라요. 요청한 템포 변화에 맞는지 이 구간을 들어 보세요.",
        value: Number.isFinite(observed.minimumBpm) && Number.isFinite(observed.maximumBpm)
          ? `자동 추정 ${Number(observed.minimumBpm.toFixed(1))}–${Number(observed.maximumBpm.toFixed(1))} BPM` : null,
      };
    case "loudness_shift":
      return {
        label: "구간별 음량 변화",
        message: "음량이 갑자기 변하는 부분이 있어요. 의도한 편곡 변화인지 이 구간을 들어 보세요.",
        value: Number.isFinite(observed.differenceDb) ? `${observed.differenceDb.toFixed(1)} dB 변화` : null,
      };
    case "spectral_shift":
      return { label: "구간별 소리 색 변화", message: "소리의 주파수 균형이 갑자기 변하는 부분이 있어요. 의도한 악기나 구간 변화인지 들어 보세요.", value: null };
    case "beat_timing_instability_suspected":
      return {
        label: warning ? "박자 불안정 의심" : "박자 변화 · 의도 확인 필요",
        message: warning ? "분리한 리듬 성분의 추정 박자 간격이 불안정해요. 연주 방식과 분리 오차를 포함해 이 구간을 들어서 확인해 주세요."
          : "추정한 박자 간격이 변하는 부분이에요. 요청한 템포와 그루브 변화에 맞는지 이 구간을 들어서 확인해 주세요.",
        value: Number.isFinite(observed.timingDeviationP95Milliseconds) ? `추정 간격 편차 ${observed.timingDeviationP95Milliseconds.toFixed(1)} ms` : null,
      };
    case "repeated_hit_timing_shift_suspected":
      return {
        label: "타격 시각 흔들림 의심",
        message: "연속된 여러 타격이 곡 안의 다른 반복보다 이르거나 늦게 나와요. 의도한 변주일 수 있으니 이 구간을 직접 들어서 확인해 주세요.",
        value: Number.isFinite(observed.peakDeviationMilliseconds) ? `가장 크게 어긋난 타격 ${observed.peakDeviationMilliseconds.toFixed(0)} ms` : null,
      };
    case "smooth_timing_change_observed":
      return {
        label: "완만한 템포 변화 관찰",
        message: "타격이 반복 기준에서 서서히 벗어났다 돌아오는 구간이에요. 급격한 흔들림은 아니에요. 의도한 템포 변화인지 들어 보세요.",
        value: Number.isFinite(observed.peakExpectedDisplacementMilliseconds) ? `가장 크게 벗어난 지점 ${observed.peakExpectedDisplacementMilliseconds.toFixed(0)} ms` : null,
      };
    case "backing_dropout_suspected":
      return {
        label: "반주 끊김 의심",
        message: "목소리나 전체 음원이 이어지는 동안 분리된 반주가 약해진 구간이에요. 분리 오차나 의도한 편곡일 수 있어 직접 들어서 확인해 주세요.",
        value: Number.isFinite(observed.relativeDepthDb) ? `주변보다 ${observed.relativeDepthDb.toFixed(1)} dB 작음` : null,
      };
    case "possible_arrangement_break":
      return { label: "편곡 쉼 가능성 · 의도 확인 필요", message: "반주가 줄거나 비는 구간을 관찰했어요. 의도한 편곡 쉼인지 자동으로 확정하지 못했어요. 이 구간을 들어 보세요.", value: null };
    case "percussive_gap_observed":
      return { label: "타격 소리 쉼 관찰", message: "추정한 타격 성분의 반복 소리가 비는 구간이에요. 의도한 드럼 쉼인지 들어 보세요.",
        value: Number.isFinite(observed.gapSeconds) ? `${observed.gapSeconds.toFixed(2)}초` : null };
    case "mix_dropout_suspected":
      return { label: warning ? "전체 신호 끊김 의심" : "선언한 쉼과 일치",
        message: warning ? "곡 중간에서 전체 신호가 거의 0이 됐다 급격히 돌아왔어요. 의도된 편집인지 자동으로 확정하지 못했어요."
          : "관측한 무음 구간 전체가 선언한 쉼 계획에 들어 있어요. 오류가 없거나 청취가 승인됐다는 뜻은 아니에요.",
        value: Number.isFinite(observed.dropDurationSeconds) ? `${observed.dropDurationSeconds.toFixed(2)}초` : null };
    default:
      return { label: finding.check, message: finding.message, value: null };
  }
}

const modelNames: Record<string, string> = {
  "acestep-v15-xl-turbo": "ACE-Step XL turbo",
  "acestep-v15-turbo": "ACE-Step turbo",
};

// Names the DiT the server reports; an unknown checkpoint is shown as it is.
export function modelLabel(model: string | null): string {
  return model ? modelNames[model] ?? model : "엔진이 켜지면 표시돼요";
}
