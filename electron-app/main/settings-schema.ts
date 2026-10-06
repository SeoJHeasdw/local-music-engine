import path from "node:path";
import { ACE_DIT_MODELS, type AceDitModel, type AssistantKind, type Settings, type Strength } from "../shared.ts";
import { requireLoopbackUrl } from "./files.ts";

export const ACE_DEFAULT_MODEL: AceDitModel = "acestep-v15-xl-turbo";
export const ACE_LM_MODEL = "acestep-5Hz-lm-4B";
export const ACE_BASE_URL = "http://127.0.0.1:18001";
// ACE accepts longer songs, but XL's 3-minute repaint already peaked at 37.8 GB on 36 GB.
export const ACE_MAX_DURATION = 300;

export function defaultSettings(projectsDir: string): Settings {
  return {
    schemaVersion: 3, engine: "ace-step", projectsDir,
    engineBaseUrl: ACE_BASE_URL, engineAutoStart: true, musicModel: ACE_DEFAULT_MODEL,
    defaultVersions: 1, defaultDurationSeconds: 60, feedbackStrength: "medium",
    assistant: { kind: "rules", baseUrl: "http://127.0.0.1:11434", model: "" }, lastExportDir: null,
  };
}

const clamp = (value: unknown, min: number, max: number, fallback: number) => {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(max, Math.max(min, Math.round(number))) : fallback;
};

const aceModel = (value: unknown): AceDitModel | null =>
  (ACE_DIT_MODELS as readonly string[]).includes(value as string) ? value as AceDitModel : null;

// Version 1 ran ACE (aceBaseUrl/ditModel), version 2 ran Music 3 on another port. Upgrade
// only the app's preferences; project files and existing audio never pass through this path.
export function normalizeSettings(raw: Record<string, unknown>, base: Settings): Settings {
  const current = raw.schemaVersion === 3 && raw.engine === "ace-step";
  const fromAce = !current && raw.schemaVersion !== 2 && raw.engine !== "minimax-music3";
  const assistantRaw = raw.assistant && typeof raw.assistant === "object" && !Array.isArray(raw.assistant)
    ? raw.assistant as Record<string, unknown> : {};
  const kind = (["rules", "ollama", "openai"] as const).includes(assistantRaw.kind as AssistantKind)
    ? assistantRaw.kind as AssistantKind : base.assistant.kind;
  const strengthValue = raw.feedbackStrength ?? raw.repaintStrength;
  const strength = (["light", "medium", "strong"] as const).includes(strengthValue as Strength)
    ? strengthValue as Strength : base.feedbackStrength;
  const projectsDir = typeof raw.projectsDir === "string" && path.isAbsolute(raw.projectsDir)
    ? path.normalize(raw.projectsDir) : base.projectsDir;
  // A Music 3 address points at that server's port; an ACE address carries over.
  const url = current ? raw.engineBaseUrl : fromAce ? raw.aceBaseUrl : undefined;
  return {
    schemaVersion: 3, engine: "ace-step", projectsDir,
    engineBaseUrl: url === undefined ? (current ? base.engineBaseUrl : ACE_BASE_URL) : requireLoopbackUrl(url, "음악 엔진"),
    engineAutoStart: typeof raw.engineAutoStart === "boolean" ? raw.engineAutoStart
      : typeof raw.aceAutoStart === "boolean" ? raw.aceAutoStart : base.engineAutoStart,
    // Only the listed checkpoints: another name could load a DiT this build never checked.
    // Upgrades start on XL, the version chosen by listening, whatever ran before.
    musicModel: current ? aceModel(raw.musicModel) ?? base.musicModel : ACE_DEFAULT_MODEL,
    defaultVersions: clamp(raw.defaultVersions, 1, 4, base.defaultVersions),
    defaultDurationSeconds: clamp(raw.defaultDurationSeconds, 10, ACE_MAX_DURATION, base.defaultDurationSeconds),
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
