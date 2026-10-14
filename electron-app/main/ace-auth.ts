import { constants } from "node:fs";
import { open } from "node:fs/promises";
import path from "node:path";
import { engineRoot } from "./paths.ts";

// Never pass this credential through preload, settings, logs or renderer events.
function keyPath(): string {
  const configured = process.env.MUSIC_ENGINE_ACE_API_KEY_FILE;
  return configured ? path.resolve(configured.replace(/^~(?=\/|$)/, process.env.HOME ?? "~")) : path.join(engineRoot, ".runtime", "ace-api-key");
}

function validate(value: string): string {
  const key = value.trim();
  if (!key || key.length > 4096 || !/^[\x21-\x7e]+$/.test(key)) throw new Error("음악 엔진 인증 정보가 올바르지 않아요.");
  return key;
}

async function readKey(): Promise<string | null> {
  for (const name of ["MUSIC_ENGINE_ACE_API_KEY", "ACESTEP_API_KEY"]) {
    if (process.env[name] !== undefined) return validate(process.env[name]!);
  }
  let handle;
  try {
    handle = await open(keyPath(), constants.O_RDONLY | constants.O_NOFOLLOW);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
  try {
    const metadata = await handle.stat();
    if (!metadata.isFile() || (metadata.mode & 0o077) || metadata.uid !== process.getuid?.()) throw new Error("음악 엔진 인증 파일은 이 사용자의 비공개 파일이어야 해요.");
    if (metadata.size > 4097) throw new Error("음악 엔진 인증 정보가 올바르지 않아요.");
    return validate(await handle.readFile("ascii"));
  } finally {
    await handle.close();
  }
}

export async function aceAuthHeaders(): Promise<Record<string, string>> {
  const key = await readKey();
  return key === null ? {} : { Authorization: `Bearer ${key}` };
}
