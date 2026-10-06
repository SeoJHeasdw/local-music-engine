import { access, mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import type { Settings } from "../shared.ts";
import { writeJsonAtomic } from "./files.ts";
import { normalizeSettings } from "./settings-schema.ts";

// Each engine change keeps the first file it replaced, next to settings.json.
function backupName(raw: Record<string, unknown>): string {
  return raw.schemaVersion === 2 || raw.engine === "minimax-music3" ? "settings-before-ace-xl.json" : "settings-before-music3.json";
}

export async function loadAndMigrateSettings(file: string, base: Settings): Promise<Settings> {
  let raw: Record<string, unknown> = {};
  try {
    const value: unknown = JSON.parse(await readFile(file, "utf8"));
    if (value && typeof value === "object" && !Array.isArray(value)) raw = value as Record<string, unknown>;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw new Error("설정 파일을 읽지 못했어요. 기존 파일을 확인해 주세요.");
  }
  const next = normalizeSettings(raw, base);
  if (raw.schemaVersion !== 3 || raw.engine !== "ace-step") {
    await mkdir(path.dirname(file), { recursive: true });
    if (Object.keys(raw).length) {
      const backup = path.join(path.dirname(file), backupName(raw));
      let exists = true;
      try { await access(backup); } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        exists = false;
      }
      if (!exists) await writeJsonAtomic(backup, raw);
    }
    await writeJsonAtomic(file, next);
  }
  return next;
}
