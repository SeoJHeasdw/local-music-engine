import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { writeJsonAtomic } from "../main/files.ts";

test("simultaneous writers publish whole JSON documents without sharing temp files", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "music-settings-"));
  try {
    const file = path.join(directory, "settings.json");
    await Promise.all(Array.from({ length: 24 }, (_, index) => writeJsonAtomic(file, { index, text: String(index).repeat(10000) })));
    const saved = JSON.parse(await readFile(file, "utf8"));
    assert.equal(saved.text, String(saved.index).repeat(10000));
    assert.deepEqual(await readdir(directory), ["settings.json"]);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
