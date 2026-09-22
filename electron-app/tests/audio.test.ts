import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { audioUrlFor, handleArtifactRequest, wavPeaks } from "../main/audio.ts";

test("artifact tokens stream full files, ranges and suffixes without exposing paths", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "music-audio-"));
  try {
    const file = path.join(directory, "fixture.wav");
    const data = Buffer.from("0123456789");
    await writeFile(file, data);
    const url = audioUrlFor(file);
    assert.equal(audioUrlFor(file), url);
    assert.ok(!url.includes(file));
    for (const [range, status, expected] of [[null, 200, "0123456789"], ["bytes=2-5", 206, "2345"], ["bytes=-3", 206, "789"], ["bytes=8-99", 206, "89"]] as const) {
      const response = await handleArtifactRequest(new Request(url, { headers: range ? { range } : {} }));
      assert.equal(response.status, status);
      assert.equal(await response.text(), expected);
    }
    for (const range of ["bytes=10-", "bytes=8-2", "bytes=-0"]) {
      const response = await handleArtifactRequest(new Request(url, { headers: { range } }));
      assert.equal(response.status, 416);
      assert.equal(response.headers.get("content-range"), "bytes */10");
    }
    assert.equal((await handleArtifactRequest(new Request("music-artifact://artifact/unknown"))).status, 404);
    assert.deepEqual(await wavPeaks(file), []);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
