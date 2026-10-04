import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { handleArtifactRequest } from "../main/audio.ts";
import { loadSong, qualityFromStatus, versionFilePath } from "../main/songs.ts";
import { qualityLabel } from "../renderer/quality.ts";

test("in-progress raw quality metadata is normalized before renderer access", () => {
  const report = qualityFromStatus({ complete: false, preferred: false, status: "unknown", summary: "자동 검사를 진행하고 있어요.", attempt: 1, maxAttempts: 4 });
  assert.equal(report?.lyrics.status, "unknown");
  assert.deepEqual(report?.retryReasons, []);
  assert.deepEqual(report?.audio, {});
  assert.equal(report?.processing, null);
  assert.equal(qualityLabel(report)?.label, "자동 검사 중");
});

test("song loading exposes a separate recommendation and streams the finished artifact while preserving raw audio and review", async () => {
  const folder = await mkdtemp(path.join(tmpdir(), "music-quality-song-"));
  try {
    const original = path.join(folder, "original.wav");
    const finished = path.join(folder, "finished.wav");
    await writeFile(original, "preserved raw");
    await writeFile(finished, "separate finished artifact");
    const review = { status: "unreviewed", rating: null, notes: [], updatedAt: null };
    const quality = { status: "passed", summary: "자동 검사 통과", groupId: "batch-slot", attempt: 2, maxAttempts: 4,
      originalSeed: 11, preferred: false, score: 98, retryReasons: [], audio: { automaticStatus: "passed" },
      lyrics: { status: "pass", orderedCoverage: 1 }, processing: null };
    const candidate = { candidateId: "raw", selected: false, seed: 12, taskType: "text2music", parentCandidateId: null, editRange: null,
      durationSeconds: 10, artifactValid: true, artifactValidation: "verified", humanReview: review, findings: [],
      stylePrompt: "piano", lyrics: "가사", model: "local", bpm: null, keyScale: null, repaintStrength: null,
      instruction: null, feedbackId: null, createdAt: null, path: original, quality, recommended: false };
    const raw = { projectId: "project", title: "곡", path: folder, createdAt: null, updatedAt: null,
      inputs: { stylePrompt: "piano", lyrics: "가사", durationSeconds: 10, structure: null, bpm: null, keyScale: null, timeSignature: null },
      selectedCandidateId: null, recommendedCandidateId: "finished", canUndoSelection: false, generationActive: false, feedback: [], jobs: [], exports: [],
      candidates: [candidate, { ...candidate, candidateId: "finished", parentCandidateId: "raw", taskType: "postprocess", path: finished,
        quality: { ...quality, preferred: true, processing: { sourceCandidateId: "raw", gain: 0.95 } }, recommended: true }] };
    const state = await loadSong(folder, async <T>() => raw as unknown as T);
    assert.equal(state.status, "ready");
    assert.equal(state.song?.recommendedVersionId, "finished");
    assert.equal(state.song?.finalVersionId, null);
    const winner = state.song!.versions.find((version) => version.id === "finished")!;
    assert.equal(winner.review.status, "unreviewed");
    assert.equal(winner.isFinal, false);
    assert.equal(winner.recommended, true);
    assert.equal(winner.quality?.attempt, 2);
    assert.equal(versionFilePath("raw"), original);
    assert.equal(versionFilePath("finished"), finished);
    assert.equal(await (await handleArtifactRequest(new Request(winner.audioUrl!))).text(), "separate finished artifact");
    assert.equal(await (await handleArtifactRequest(new Request(state.song!.versions[0]!.audioUrl!))).text(), "preserved raw");
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});
