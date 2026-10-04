import path from "node:path";
import type { AssistantKind, Settings, Strength } from "../shared.ts";
import { requireLoopbackUrl } from "./files.ts";

export const MUSIC3_MODEL = "mlx-community/MiniMax-Music3-mxfp8";
export const MUSIC3_BASE_URL = "http://127.0.0.1:18002";
export const MUSIC3_MAX_DURATION = 300;

export function defaultSettings(projectsDir: string): Settings {
  return {
    schemaVersion: 2, engine: "minimax-music3", projectsDir,
    engineBaseUrl: MUSIC3_BASE_URL, engineAutoStart: true, musicModel: MUSIC3_MODEL,
    defaultVersions: 1, defaultDurationSeconds: 60, feedbackStrength: "medium",
    assistant: { kind: "rules", baseUrl: "http://127.0.0.1:11434", model: "" }, lastExportDir: null,
  };
}

const clamp = (value: unknown, min: number, max: number, fallback: number) => {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(max, Math.max(min, Math.round(number))) : fallback;
};

// Version 1 contained ACE-specific model and connection settings. Upgrade only the
// app's preferences; project files and existing audio never pass through this path.
export function normalizeSettings(raw: Record<string, unknown>, base: Settings): Settings {
  const legacy = raw.schemaVersion !== 2 || raw.engine !== "minimax-music3";
  const assistantRaw = raw.assistant && typeof raw.assistant === "object" && !Array.isArray(raw.assistant)
    ? raw.assistant as Record<string, unknown> : {};
  const kind = (["rules", "ollama", "openai"] as const).includes(assistantRaw.kind as AssistantKind)
    ? assistantRaw.kind as AssistantKind : base.assistant.kind;
  const strengthValue = raw.feedbackStrength ?? raw.repaintStrength;
  const strength = (["light", "medium", "strong"] as const).includes(strengthValue as Strength)
    ? strengthValue as Strength : base.feedbackStrength;
  const projectsDir = typeof raw.projectsDir === "string" && path.isAbsolute(raw.projectsDir)
    ? path.normalize(raw.projectsDir) : base.projectsDir;
  return {
    schemaVersion: 2, engine: "minimax-music3", projectsDir,
    engineBaseUrl: legacy || raw.engineBaseUrl === undefined
      ? (legacy ? MUSIC3_BASE_URL : base.engineBaseUrl) : requireLoopbackUrl(raw.engineBaseUrl, "음악 엔진"),
    engineAutoStart: typeof raw.engineAutoStart === "boolean" ? raw.engineAutoStart
      : legacy && typeof raw.aceAutoStart === "boolean" ? raw.aceAutoStart : base.engineAutoStart,
    // This build has one supported Music3 runtime. Arbitrary model names could load
    // an incompatible implementation; model variants need an explicit future update.
    musicModel: MUSIC3_MODEL,
    defaultVersions: clamp(raw.defaultVersions, 1, 4, base.defaultVersions),
    defaultDurationSeconds: clamp(raw.defaultDurationSeconds, 10, MUSIC3_MAX_DURATION, base.defaultDurationSeconds),
    feedbackStrength: strength,
    assistant: {
      kind,
      baseUrl: assistantRaw.baseUrl === undefined ? base.assistant.baseUrl : requireLoopbackUrl(assistantRaw.baseUrl, "도우미 LLM"),
      model: typeof assistantRaw.model === "string" ? assistantRaw.model.trim().slice(0, 200) : base.assistant.model,
    },
    lastExportDir: typeof raw.lastExportDir === "string" && path.isAbsolute(raw.lastExportDir)
      ? raw.lastExportDir : raw.lastExportDir === null ? null : base.lastExportDir,
  };
}
