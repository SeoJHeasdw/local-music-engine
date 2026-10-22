import type { ExportVariant, Plan, Settings, SongPurpose } from "../shared.ts";
import { ACE_LM_MODEL } from "./settings-schema.ts";

export function aceGenerationArgs(folder: string, seeds: string, settings: Settings): string[] {
  return ["generate", folder, "--seeds", seeds, "--engine", "ace-step", "--base-url", settings.engineBaseUrl,
    "--model", settings.musicModel, "--lm-model", ACE_LM_MODEL];
}

// The repaint uses the DiT the app runs, not the parent's: an older turbo version can be
// edited by XL without restarting the engine, and the request records which one ran.
export function aceRepaintArgs(folder: string, versionId: string, range: { startSeconds: number; endSeconds: number },
  seed: string, settings: Settings): string[] {
  return ["repaint", folder, "--engine", "ace-step", "--candidate-id", versionId,
    "--start", range.startSeconds.toFixed(2), "--end", range.endSeconds.toFixed(2), "--seed", seed,
    "--model", settings.musicModel, "--base-url", settings.engineBaseUrl];
}

export function requirePlanAction(plan: Pick<Plan, "action" | "range">): void {
  if (plan.action === "regenerate" && !plan.range) return;
  const range = plan.range;
  if (plan.action === "repaint" && range && Number.isFinite(range.startSeconds) && Number.isFinite(range.endSeconds)
    && range.startSeconds >= 0 && range.endSeconds > range.startSeconds) return;
  throw new Error(plan.action === "repaint" ? "고칠 구간을 파형에서 골라 주세요." : "알 수 없는 수정 방식이에요.");
}

// IPC values are untrusted: only the two fixed choices pass.
export function parseExportVariant(value: unknown): ExportVariant {
  if (value === undefined || value === "song") return "song";
  if (value === "loop") return "loop";
  throw new Error("알 수 없는 내보내기 종류예요.");
}

export function parseSongPurpose(value: unknown): SongPurpose | null {
  if (value === undefined || value === null) return null;
  if (value === "bgm") return "bgm";
  throw new Error("알 수 없는 곡 용도예요.");
}

export const BGM_MIN_SECONDS = 60;

// A BGM song has no lyrics, and needs room for an intro, 16 looped bars and an ending.
export function requireBgmInput(purpose: SongPurpose | null, lyrics: string, durationSeconds: number): void {
  if (purpose !== "bgm") return;
  if (lyrics !== "[Instrumental]") throw new Error("BGM은 가사 없이 만들어요.");
  if (!(durationSeconds >= BGM_MIN_SECONDS)) throw new Error("BGM은 1분 이상으로 만들어 주세요.");
}
