import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm, chmod, stat, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { aceAuthHeaders, ensureAceApiKey } from "../main/ace-auth.ts";

test("ACE credential is private, published once, and consumed only by main", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "music-auth-"));
  const saved = { file: process.env.MUSIC_ENGINE_ACE_API_KEY_FILE, music: process.env.MUSIC_ENGINE_ACE_API_KEY, ace: process.env.ACESTEP_API_KEY };
  process.env.MUSIC_ENGINE_ACE_API_KEY_FILE = path.join(directory, "key");
  delete process.env.MUSIC_ENGINE_ACE_API_KEY;
  delete process.env.ACESTEP_API_KEY;
  try {
    assert.deepEqual(await aceAuthHeaders(), {});
    await Promise.all(Array.from({ length: 16 }, () => ensureAceApiKey()));
    const key = (await readFile(path.join(directory, "key"), "ascii")).trim();
    assert.match(key, /^[a-f0-9]{64}$/);
    assert.deepEqual(await aceAuthHeaders(), { Authorization: `Bearer ${key}` });
    assert.equal((await stat(path.join(directory, "key"))).mode & 0o777, 0o600);
    assert.deepEqual(await readdir(directory), ["key"]);
    process.env.MUSIC_ENGINE_ACE_API_KEY = "configured-local-key";
    await ensureAceApiKey();
    assert.deepEqual(await aceAuthHeaders(), { Authorization: "Bearer configured-local-key" });
    delete process.env.MUSIC_ENGINE_ACE_API_KEY;
    await chmod(path.join(directory, "key"), 0o644);
    await assert.rejects(ensureAceApiKey, /비공개/);
    await unlink(path.join(directory, "key"));
    const outside = path.join(directory, "outside");
    await writeFile(outside, "original", { mode: 0o600 });
    await symlink(outside, path.join(directory, "key"));
    await assert.rejects(ensureAceApiKey);
    assert.equal(await readFile(outside, "ascii"), "original");
  } finally {
    for (const [name, value] of [["MUSIC_ENGINE_ACE_API_KEY_FILE", saved.file], ["MUSIC_ENGINE_ACE_API_KEY", saved.music], ["ACESTEP_API_KEY", saved.ace]]) {
      if (value === undefined) delete process.env[name!]; else process.env[name!] = value;
    }
    await rm(directory, { recursive: true, force: true });
  }
});
