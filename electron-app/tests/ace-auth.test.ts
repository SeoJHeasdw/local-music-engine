import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, chmod, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { aceAuthHeaders } from "../main/ace-auth.ts";

// The ACE server publishes the key it serves with (scripts/ace_api_server.py); main only reads it.
test("ACE credential is read from a private file or the environment and consumed only by main", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "music-auth-"));
  const saved = { file: process.env.MUSIC_ENGINE_ACE_API_KEY_FILE, music: process.env.MUSIC_ENGINE_ACE_API_KEY, ace: process.env.ACESTEP_API_KEY };
  const file = path.join(directory, "key");
  process.env.MUSIC_ENGINE_ACE_API_KEY_FILE = file;
  delete process.env.MUSIC_ENGINE_ACE_API_KEY;
  delete process.env.ACESTEP_API_KEY;
  try {
    assert.deepEqual(await aceAuthHeaders(), {});
    const key = "a1".repeat(32);
    await writeFile(file, `${key}\n`, { mode: 0o600 });
    assert.deepEqual(await aceAuthHeaders(), { Authorization: `Bearer ${key}` });
    process.env.MUSIC_ENGINE_ACE_API_KEY = "configured-local-key";
    assert.deepEqual(await aceAuthHeaders(), { Authorization: "Bearer configured-local-key" });
    delete process.env.MUSIC_ENGINE_ACE_API_KEY;
    await chmod(file, 0o644);
    await assert.rejects(aceAuthHeaders, /비공개/);
    await unlink(file);
    const outside = path.join(directory, "outside");
    await writeFile(outside, "original", { mode: 0o600 });
    await symlink(outside, file);
    await assert.rejects(aceAuthHeaders);
    assert.equal(await readFile(outside, "ascii"), "original");
  } finally {
    for (const [name, value] of [["MUSIC_ENGINE_ACE_API_KEY_FILE", saved.file], ["MUSIC_ENGINE_ACE_API_KEY", saved.music], ["ACESTEP_API_KEY", saved.ace]]) {
      if (value === undefined) delete process.env[name!]; else process.env[name!] = value;
    }
    await rm(directory, { recursive: true, force: true });
  }
});
