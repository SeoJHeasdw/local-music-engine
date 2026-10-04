import type { EngineStatus, Plan, Settings } from "../shared.ts";

export function music3GenerationArgs(folder: string, seeds: string, settings: Settings): string[] {
  return ["generate", folder, "--seeds", seeds, "--engine", "minimax-music3", "--base-url", settings.engineBaseUrl, "--model", settings.musicModel];
}

export function requireFullGeneration(plan: Pick<Plan, "action" | "range">): void {
  if (plan.action !== "regenerate" || plan.range) throw new Error("Music 3는 곡 전체의 새 버전만 만들 수 있어요. 구간 수정은 지원하지 않아요.");
}

export function music3Ready(status: EngineStatus): boolean {
  return status.engine === "minimax-music3" && status.capabilities.text2music
    && (status.state === "ready" || status.state === "external");
}
