import { open, rename, unlink } from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";

// Same rule as project.json: write a sibling temp file, fsync, then rename over the target.
export async function writeJsonAtomic(destination: string, value: unknown): Promise<void> {
  const temporary = path.join(
    path.dirname(destination),
    `.${path.basename(destination)}.${randomUUID()}.tmp`,
  );
  try {
    const handle = await open(temporary, "wx", 0o600);
    try {
      await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`, "utf8");
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(temporary, destination);
    const directory = await open(path.dirname(destination), "r");
    try {
      await directory.sync();
    } finally {
      await directory.close();
    }
  } finally {
    await unlink(temporary).catch(() => undefined);
  }
}

export function requireLoopbackUrl(input: unknown, label: string): string {
  let url: URL;
  try {
    url = new URL(String(input ?? "").trim());
  } catch {
    throw new Error(`${label} 주소 형식이 올바르지 않아요.`);
  }
  const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  if (url.protocol !== "http:" || !loopback) {
    throw new Error(`${label}는 이 Mac 안의 주소(http://127.0.0.1:…)만 쓸 수 있어요.`);
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error(`${label} 주소에는 계정·쿼리·해시를 넣을 수 없어요.`);
  }
  return `${url.protocol}//${url.host}${url.pathname}`.replace(/\/+$/, "");
}
