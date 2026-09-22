import type { Finding, ReviewStatus, Song, Strength, TimeRange, Version } from "../shared.ts";

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
  listened: { label: "애매해요", short: "애매" },
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
  for (const version of song.versions) {
    const parent = version.parentId ? byId.get(version.parentId) : undefined;
    if (!parent) {
      fullIndex += 1;
      const node = { version, name: `버전 ${fullIndex}`, short: `버전 ${fullIndex}`, depth: 0, children: [] };
      byId.set(version.id, node);
      roots.push(node);
    } else {
      const index = parent.children.length + 1;
      const short = `수정 ${index}`;
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
  return { roots, flat, byId };
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
    default:
      return { label: finding.check, message: finding.message, value: null };
  }
}
