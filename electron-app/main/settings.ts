import { app } from "electron";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import type { AssistantKind, Settings, Strength } from "../shared.ts";
import { requireLoopbackUrl, writeJsonAtomic } from "./files.ts";
import { defaultProjectsDir } from "./paths.ts";

export type AppState = {
  lastSongPath: string | null;
  // Songs opened from outside the projects folder stay in the library list.
  extraSongPaths: string[];
};

function defaults(): Settings {
  return {
    projectsDir: defaultProjectsDir,
    aceBaseUrl: "http://127.0.0.1:18001",
    aceAutoStart: true,
    ditModel: "acestep-v15-turbo",
    lmModel: "acestep-5Hz-lm-0.6B",
    defaultVersions: 2,
    defaultDurationSeconds: 120,
    repaintStrength: "medium",
    assistant: { kind: "rules", baseUrl: "http://127.0.0.1:11434", model: "" },
    lastExportDir: null,
  };
}

const settingsFile = () => path.join(app.getPath("userData"), "settings.json");
const stateFile = () => path.join(app.getPath("userData"), "state.json");

let settings: Settings | null = null;
let state: AppState | null = null;
let settingsWrites: Promise<unknown> = Promise.resolve();
let stateWrites: Promise<unknown> = Promise.resolve();

async function readJson(file: string): Promise<Record<string, unknown> | null> {
  try {
    const value = JSON.parse(await readFile(file, "utf8")) as unknown;
    return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

const clamp = (value: unknown, min: number, max: number, fallback: number) => {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(max, Math.max(min, Math.round(number))) : fallback;
};

function normalize(raw: Record<string, unknown>, base: Settings): Settings {
  const assistantRaw = (raw.assistant ?? {}) as Record<string, unknown>;
  const kind = (["rules", "ollama", "openai"] as const).includes(assistantRaw.kind as AssistantKind)
    ? (assistantRaw.kind as AssistantKind)
    : base.assistant.kind;
  const strength = (["light", "medium", "strong"] as const).includes(raw.repaintStrength as Strength)
    ? (raw.repaintStrength as Strength)
    : base.repaintStrength;
  const projectsDir =
    typeof raw.projectsDir === "string" && path.isAbsolute(raw.projectsDir)
      ? path.normalize(raw.projectsDir)
      : base.projectsDir;
  const text = (value: unknown, fallback: string) =>
    typeof value === "string" && value.trim() ? value.trim().slice(0, 200) : fallback;
  return {
    projectsDir,
    aceBaseUrl:
      raw.aceBaseUrl === undefined ? base.aceBaseUrl : requireLoopbackUrl(raw.aceBaseUrl, "음악 엔진"),
    aceAutoStart: typeof raw.aceAutoStart === "boolean" ? raw.aceAutoStart : base.aceAutoStart,
    ditModel: text(raw.ditModel, base.ditModel),
    lmModel: text(raw.lmModel, base.lmModel),
    defaultVersions: clamp(raw.defaultVersions, 1, 4, base.defaultVersions),
    defaultDurationSeconds: clamp(raw.defaultDurationSeconds, 10, 600, base.defaultDurationSeconds),
    repaintStrength: strength,
    assistant: {
      kind,
      baseUrl:
        assistantRaw.baseUrl === undefined
          ? base.assistant.baseUrl
          : requireLoopbackUrl(assistantRaw.baseUrl, "도우미 LLM"),
      model: typeof assistantRaw.model === "string" ? assistantRaw.model.trim().slice(0, 200) : base.assistant.model,
    },
    lastExportDir:
      typeof raw.lastExportDir === "string" && path.isAbsolute(raw.lastExportDir)
        ? raw.lastExportDir
        : raw.lastExportDir === null
          ? null
          : base.lastExportDir,
  };
}

export async function loadSettings(): Promise<Settings> {
  if (settings) return settings;
  const raw = (await readJson(settingsFile())) ?? {};
  try {
    settings = normalize(raw, defaults());
  } catch {
    settings = defaults();
  }
  return settings;
}

export function currentSettings(): Settings {
  if (!settings) throw new Error("settings are not loaded");
  return settings;
}

export function saveSettings(partial: Partial<Settings>): Promise<Settings> {
  const write = settingsWrites.then(() => writeSettings(partial));
  settingsWrites = write.catch(() => undefined);
  return write;
}

async function writeSettings(partial: Partial<Settings>): Promise<Settings> {
  const current = await loadSettings();
  const merged = {
    ...current,
    ...partial,
    assistant: { ...current.assistant, ...(partial.assistant ?? {}) },
  } as unknown as Record<string, unknown>;
  const next = normalize(merged, current);
  await mkdir(path.dirname(settingsFile()), { recursive: true });
  await writeJsonAtomic(settingsFile(), next);
  settings = next;
  return next;
}

export async function loadAppState(): Promise<AppState> {
  if (state) return state;
  const raw = (await readJson(stateFile())) ?? {};
  // The first app version stored only { projectPath }.
  const last = typeof raw.lastSongPath === "string" ? raw.lastSongPath : raw.projectPath;
  state = {
    lastSongPath: typeof last === "string" ? last : null,
    extraSongPaths: Array.isArray(raw.extraSongPaths)
      ? raw.extraSongPaths.filter((item): item is string => typeof item === "string").slice(0, 50)
      : [],
  };
  return state;
}

export function updateAppState(change: Partial<AppState>): Promise<AppState> {
  const write = stateWrites.then(async () => {
    const next = { ...(await loadAppState()), ...change };
    await mkdir(path.dirname(stateFile()), { recursive: true });
    await writeJsonAtomic(stateFile(), next);
    state = next;
    return next;
  });
  stateWrites = write.catch(() => undefined);
  return write;
}
