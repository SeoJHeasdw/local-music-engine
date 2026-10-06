import { app } from "electron";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import type { Settings } from "../shared.ts";
import { writeJsonAtomic } from "./files.ts";
import { defaultProjectsDir } from "./paths.ts";
import { defaultSettings, normalizeSettings } from "./settings-schema.ts";
import { loadAndMigrateSettings } from "./settings-storage.ts";
import { parseWindowState, type WindowState } from "./window-state.ts";

export type AppState = {
  lastSongPath: string | null;
  // Songs opened from outside the projects folder stay in the library list.
  extraSongPaths: string[];
  window: WindowState | null;
};

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

export async function loadSettings(): Promise<Settings> {
  if (settings) return settings;
  settings = await loadAndMigrateSettings(settingsFile(), defaultSettings(defaultProjectsDir));
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
  const next = normalizeSettings(merged, current);
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
    window: parseWindowState(raw.window),
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
