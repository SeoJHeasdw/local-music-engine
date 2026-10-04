import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm, chmod, stat, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { music3AuthHeaders, ensureMusic3ApiKey } from "../main/music3-auth.ts";

test("Music3 credential is private, published once, and consumed only by main", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "music-auth-"));
  const saved = { file: process.env.MUSIC_ENGINE_MUSIC3_API_KEY_FILE, music: process.env.MUSIC_ENGINE_MUSIC3_API_KEY };
  process.env.MUSIC_ENGINE_MUSIC3_API_KEY_FILE = path.join(directory, "key");
  delete process.env.MUSIC_ENGINE_MUSIC3_API_KEY;
  try {
    assert.deepEqual(await music3AuthHeaders(), {});
    await Promise.all(Array.from({ length: 16 }, () => ensureMusic3ApiKey()));
    const key = (await readFile(path.join(directory, "key"), "ascii")).trim();
    assert.match(key, /^[a-f0-9]{64}$/);
    assert.deepEqual(await music3AuthHeaders(), { Authorization: `Bearer ${key}` });
    assert.equal((await stat(path.join(directory, "key"))).mode & 0o777, 0o600);
    assert.deepEqual(await readdir(directory), ["key"]);
    process.env.MUSIC_ENGINE_MUSIC3_API_KEY = "configured-local-key";
    await ensureMusic3ApiKey();
    assert.deepEqual(await music3AuthHeaders(), { Authorization: "Bearer configured-local-key" });
    delete process.env.MUSIC_ENGINE_MUSIC3_API_KEY;
    await chmod(path.join(directory, "key"), 0o644);
    await assert.rejects(ensureMusic3ApiKey, /비공개/);
    await unlink(path.join(directory, "key"));
    const outside = path.join(directory, "outside");
    await writeFile(outside, "original", { mode: 0o600 });
    await symlink(outside, path.join(directory, "key"));
    await assert.rejects(ensureMusic3ApiKey);
    assert.equal(await readFile(outside, "ascii"), "original");
  } finally {
    for (const [name, value] of [["MUSIC_ENGINE_MUSIC3_API_KEY_FILE", saved.file], ["MUSIC_ENGINE_MUSIC3_API_KEY", saved.music]]) {
      if (value === undefined) delete process.env[name!]; else process.env[name!] = value;
    }
    await rm(directory, { recursive: true, force: true });
  }
});
