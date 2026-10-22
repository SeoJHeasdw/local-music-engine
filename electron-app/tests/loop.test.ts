import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { audioUrlFor, handleArtifactRequest } from "../main/audio.ts";
import { parseExportVariant, parseSongPurpose, requireBgmInput } from "../main/generation-arguments.ts";
import { loadSong, loopFromStatus } from "../main/songs.ts";
import { emptyProductionRules, productionPreview } from "../production-rules.ts";
import { loopPosition, previewOffset } from "../renderer/loop-preview.ts";
import { loopReportLines, loopSummaryText } from "../renderer/quality.ts";
import type { ProductionCatalog } from "../shared.ts";

const ready = { status: "ready", startSeconds: 10.65, endSeconds: 74.65, durationSeconds: 64, bars: 24, bpm: 90, sampleRate: 48000, warnings: [] };

test("a verified loop reaches the renderer as an audio token, never a path", () => {
  const loop = loopFromStatus(ready, "/songs/a/artifacts/loops/job.wav");
  assert.equal(loop?.status, "ready");
  assert.ok(loop?.status === "ready" && loop.audioUrl.startsWith("music-artifact://artifact/") && !loop.audioUrl.includes("/songs/"));
  // Without a file that verified, or with a broken number, there is nothing to play.
  assert.equal(loopFromStatus(ready, ""), null);
  assert.equal(loopFromStatus({ ...ready, bars: Number.NaN }, "/x.wav"), null);
  const warned = loopFromStatus({ ...ready, warnings: ["phase_ambiguous", 3, null] }, "/x.wav");
  assert.deepEqual(warned?.status === "ready" && warned.warnings, ["phase_ambiguous"]);
  // A version without a loop says why, in the engine's words.
  assert.deepEqual(loopFromStatus({ status: "none", code: "no_kick", reason: "킥 드럼이 거의 없어요." }, ""), { status: "none", code: "no_kick", reason: "킥 드럼이 거의 없어요." });
  assert.equal(loopFromStatus({ status: "something" }, "/x.wav"), null);
  assert.equal(loopFromStatus(null, "/x.wav"), null);
});

test("only the two export kinds and the one purpose pass IPC", () => {
  assert.equal(parseExportVariant(undefined), "song");
  assert.equal(parseExportVariant("loop"), "loop");
  assert.throws(() => parseExportVariant("../../etc"), /알 수 없는 내보내기/);
  assert.equal(parseSongPurpose(undefined), null);
  assert.equal(parseSongPurpose("bgm"), "bgm");
  assert.throws(() => parseSongPurpose("podcast"), /알 수 없는 곡 용도/);
  assert.doesNotThrow(() => requireBgmInput("bgm", "[Instrumental]", 120));
  assert.throws(() => requireBgmInput("bgm", "[Verse]\n안녕", 120), /가사 없이/);
  assert.throws(() => requireBgmInput("bgm", "[Instrumental]", 30), /1분 이상/);
  assert.doesNotThrow(() => requireBgmInput(null, "[Verse]\n안녕", 30));
});

test("the seam preview starts six seconds before the wrap and keeps going around", () => {
  assert.equal(previewOffset(64), 58);
  assert.equal(previewOffset(4), 0);
  assert.equal(loopPosition(64, 58, 6), 0);
  assert.equal(loopPosition(64, 58, 10), 4);
  assert.equal(loopPosition(0, 0, 3), 0);
});

test("the loop is described in a row and in the checks tab", () => {
  const loop = loopFromStatus({ ...ready, warnings: ["phase_ambiguous"] }, "/x.wav");
  assert.equal(loopSummaryText(loop), "반복용 1분 4초 · 24마디");
  const lines = loopReportLines(loop);
  assert.match(lines[0]!.text, /10\.65초부터 74\.65초까지 24마디/);
  assert.equal(lines[1]!.tone, "warning");
  assert.equal(loopSummaryText({ status: "none", code: "no_kick", reason: "킥 드럼이 거의 없어요." }), "반복용 없음");
  assert.deepEqual(loopReportLines({ status: "none", code: "no_kick", reason: "킥 드럼이 거의 없어요." }), [{ text: "킥 드럼이 거의 없어요.", tone: "warning" }]);
  assert.equal(loopSummaryText(null), null);
});

test("the BGM words lead the preview prompt once, as the engine sends them", () => {
  const caption = "instrumental background music, no vocals";
  const catalog: ProductionCatalog = {
    version: 1, presets: [],
    rules: [{ id: "expressive-performance", label: "보컬 감정", description: "", caption: "emotional lead vocal" },
            { id: "clean-production", label: "깔끔한 음질", description: "", caption: "clean mix" }],
    purposes: [{ id: "bgm", label: "BGM", description: "", caption }],
  };
  const input = { stylePrompt: "lofi beat", instrumental: true, durationSeconds: 120, bpm: "", keyScale: "", timeSignature: "",
    productionRules: { ...emptyProductionRules(), ruleIds: ["expressive-performance", "clean-production"] }, purpose: "bgm" as const };
  const preview = productionPreview(input, catalog);
  assert.equal(preview.stylePrompt, `${caption}, lofi beat, clean mix`);
  assert.equal(preview.purpose?.label, "BGM");
  assert.equal(productionPreview({ ...input, stylePrompt: `${caption}, lofi beat` }, catalog).stylePrompt, `${caption}, lofi beat, clean mix`);
  // Without a purpose nothing is added; the vocal rule is skipped for any instrumental.
  assert.equal(productionPreview({ ...input, purpose: null }, catalog).stylePrompt, "lofi beat, clean mix");
});

test("the loop preview may fetch audio tokens from the app page", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "music-loop-"));
  try {
    const file = path.join(directory, "loop.wav");
    await writeFile(file, Buffer.from("0123456789"));
    const url = audioUrlFor(file);
    for (const headers of [{}, { range: "bytes=2-5" }] as Array<Record<string, string>>) {
      const response = await handleArtifactRequest(new Request(url, { headers }));
      assert.equal(response.headers.get("access-control-allow-origin"), "*");
    }
    const missing = await handleArtifactRequest(new Request("music-artifact://artifact/unknown"));
    assert.equal(missing.status, 404);
    assert.equal(missing.headers.get("access-control-allow-origin"), "*");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("a BGM song's status maps its purpose, its loop and which exports were loops", async () => {
  const status = {
    projectId: "p", title: "BGM", path: "/songs/bgm", createdAt: null, updatedAt: null,
    inputs: { lyrics: "[Instrumental]", stylePrompt: "lofi", durationSeconds: 120, structure: null, bpm: 90, keyScale: null, timeSignature: "4", purpose: "bgm" },
    selectedCandidateId: null, canUndoSelection: false, generationActive: false, feedback: [], jobs: [],
    candidates: [{
      candidateId: "candidate_a", selected: false, seed: 1, taskType: "postprocess", parentCandidateId: null, editRange: null,
      durationSeconds: 120, artifactValid: true, artifactValidation: "ok", humanReview: { status: "unreviewed", rating: null, notes: [], updatedAt: null },
      findings: [], stylePrompt: "lofi", lyrics: "[Instrumental]", model: null, bpm: 90, keyScale: null, repaintStrength: null,
      instruction: null, feedbackId: null, createdAt: null, path: "/songs/bgm/v.wav", purpose: "bgm", loop: ready, loopPath: "/songs/bgm/loop.wav",
    }],
    exports: [{ artifactId: "artifact_a", candidateId: "candidate_a", variant: "loop", createdAt: null, path: "/songs/bgm/e.wav", exists: true, externalPath: null, externalExists: false }],
  };
  const state = await loadSong("/songs/bgm", (async () => status) as never);
  const version = state.song!.versions[0]!;
  assert.equal(state.song!.inputs.purpose, "bgm");
  assert.equal(version.purpose, "bgm");
  assert.equal(version.loop?.status, "ready");
  assert.equal(state.song!.exports[0]!.variant, "loop");
});
