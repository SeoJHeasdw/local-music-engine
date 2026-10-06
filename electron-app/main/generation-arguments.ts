import type { EngineStatus, Plan, Settings } from "../shared.ts";
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

export function aceReady(status: EngineStatus): boolean {
  return status.engine === "ace-step" && status.capabilities.text2music
    && (status.state === "ready" || status.state === "external");
}
