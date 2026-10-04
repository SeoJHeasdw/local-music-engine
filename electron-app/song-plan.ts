import type { RegenerateSongInput, ProductionCatalog, SongPlanInput } from "./shared.ts";

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("곡 제작 요청 형식이 올바르지 않아요.");
  return value as Record<string, unknown>;
}

export function parseSongPlanInput(value: unknown, catalog: ProductionCatalog): SongPlanInput {
  const input = record(value);
  const keys = ["lyrics", "durationSeconds", "bpm", "timeSignature", "presetId", "development", "breathing", "instrumental"];
  if (Object.keys(input).some((key) => !keys.includes(key))) throw new Error("지원하지 않는 곡 전개 정보예요.");
  if (typeof input.lyrics !== "string" || input.lyrics.length > 4096) throw new Error("가사는 4,096자 안으로 적어 주세요.");
  if (typeof input.durationSeconds !== "number" || !Number.isFinite(input.durationSeconds) || input.durationSeconds < 10 || input.durationSeconds > 300) throw new Error("곡 길이는 10초에서 5분 사이여야 해요.");
  if (input.bpm !== null && (typeof input.bpm !== "number" || !Number.isInteger(input.bpm) || input.bpm < 30 || input.bpm > 300)) throw new Error("빠르기는 30~300 사이의 정수로 적어 주세요.");
  if (input.timeSignature !== null && (typeof input.timeSignature !== "string" || !["2", "3", "4", "6", "2/4", "3/4", "4/4", "6/8"].includes(input.timeSignature))) throw new Error("박자 정보가 올바르지 않아요.");
  if (input.presetId !== null && !catalog.presets.some((preset) => preset.id === input.presetId)) throw new Error("알 수 없는 제작 프리셋이에요.");
  if (["development", "breathing", "instrumental"].some((key) => typeof input[key] !== "boolean")) throw new Error("곡 전개와 가사 호흡 선택이 올바르지 않아요.");
  return { lyrics: input.lyrics, durationSeconds: input.durationSeconds, bpm: input.bpm as number | null, timeSignature: input.timeSignature as string | null,
    presetId: input.presetId as string | null, development: input.development as boolean, breathing: input.breathing as boolean, instrumental: input.instrumental as boolean };
}

export function parseRegenerateInput(value: unknown): RegenerateSongInput {
  const input = record(value);
  if (Object.keys(input).some((key) => !["songId", "versionId", "stylePrompt", "lyrics", "versions"].includes(key))) throw new Error("지원하지 않는 새 버전 정보예요.");
  if (typeof input.songId !== "string" || !/^[a-f0-9]{20}$/.test(input.songId)) throw new Error("알 수 없는 곡이에요.");
  if (typeof input.versionId !== "string" || !/^candidate_[a-f0-9]{32}$/.test(input.versionId)) throw new Error("알 수 없는 버전이에요.");
  if (typeof input.versions !== "number" || !Number.isInteger(input.versions) || input.versions < 1 || input.versions > 4) throw new Error("버전은 1~4개까지 만들 수 있어요.");
  if (input.stylePrompt !== undefined && (typeof input.stylePrompt !== "string" || input.stylePrompt.length > 1500)) throw new Error("스타일은 1,500자 안으로 적어 주세요.");
  if (input.lyrics !== undefined && (typeof input.lyrics !== "string" || !input.lyrics.trim() || input.lyrics.length > 4096)) throw new Error("가사는 4,096자 안으로 적거나 [Instrumental]을 써 주세요.");
  return { songId: input.songId, versionId: input.versionId, versions: input.versions,
    ...(input.stylePrompt === undefined ? {} : { stylePrompt: (input.stylePrompt as string).trim() }),
    ...(input.lyrics === undefined ? {} : { lyrics: (input.lyrics as string).trim() }) };
}
