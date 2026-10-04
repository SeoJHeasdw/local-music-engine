import assert from "node:assert/strict";
import { test } from "node:test";
import { parseRegenerateInput, parseSongPlanInput } from "../song-plan.ts";
import { SongPlanPreview } from "../renderer/song-plan-preview.ts";
import { productionPreview } from "../production-rules.ts";
import type { ProductionCatalog, SongPlanInput, SongPlanResult } from "../shared.ts";

const catalog: ProductionCatalog = { version: 1, presets: [{ id: "emotional-hiphop", label: "감성 힙합", description: "", caption: "hip hop", bpm: 84, keyScale: null, timeSignature: "4", ruleIds: [] }], rules: [] };
const input = (): SongPlanInput => ({ lyrics: "[Verse]\n긴 한 줄의 가사", durationSeconds: 30, bpm: null, timeSignature: null, presetId: null, development: true, breathing: true, instrumental: false });
const plan = (lyricsPrepared = "[Verse]\n긴 한 줄의\n가사"): SongPlanResult => ({ version: 1, lyricsOriginal: input().lyrics, lyricsPrepared, changes: [], arrangement: [], phrases: [], warnings: [], timing: { bpm: 96, timeSignature: "4", quarterBeatsPerBar: 4, secondsPerBar: 2.5, estimatedTotalBars: 12, bpmBeatUnit: "quarter-note" }, options: { presetId: null, development: true, breathing: true, instrumental: false }, guidance: [] });

test("song plan IPC exposes only bounded musical inputs and rejects paths and commands", () => {
  assert.deepEqual(parseSongPlanInput(input(), catalog), input());
  for (const value of [null, [], { ...input(), lyrics: 123 }, { ...input(), lyrics: "x".repeat(4097) },
    { ...input(), durationSeconds: Infinity }, { ...input(), durationSeconds: 9 }, { ...input(), durationSeconds: 301 }, { ...input(), bpm: "84" }, { ...input(), bpm: true },
    { ...input(), timeSignature: "7/8" }, { ...input(), timeSignature: {} }, { ...input(), presetId: "unknown" },
    { ...input(), breathing: 1 }, { ...input(), path: "/private/file" }, { ...input(), command: "execute" }]) assert.throws(() => parseSongPlanInput(value, catalog));
});

test("delayed planning is discarded after manual edits, including edit then restore", () => {
  const preview = new SongPlanPreview();
  const ticket = preview.begin(input());
  preview.edited();
  preview.edited();
  assert.equal(preview.complete(ticket, plan()), false);
  assert.equal(preview.current(input()), null);
});

test("newer planning request and reset cannot be overwritten by an old response", () => {
  const preview = new SongPlanPreview();
  const old = preview.begin(input());
  const latestInput = { ...input(), durationSeconds: 60 };
  const latest = preview.begin(latestInput);
  const result = plan("latest prepared lyrics");
  assert.equal(preview.complete(latest, result), true);
  assert.equal(preview.complete(old, plan()), false);
  assert.equal(preview.current(latestInput), result);
  assert.equal(preview.current(input()), null);
  preview.reset();
  assert.equal(preview.current(latestInput), null);
});

test("planning preview supplies the same neutral timing metadata as generation while explicit values win", () => {
  const draft = { stylePrompt: "warm piano", instrumental: false, durationSeconds: 30, bpm: "", keyScale: "", timeSignature: "", productionRules: { version: 1 as const, presetId: null, ruleIds: ["phrase-breathing"] } };
  assert.equal(productionPreview(draft, catalog).bpm, "96");
  assert.equal(productionPreview(draft, catalog).timeSignature, "4/4");
  const manual = productionPreview({ ...draft, bpm: "87", timeSignature: "6/8" }, catalog);
  assert.equal(manual.bpm, "87");
  assert.equal(manual.timeSignature, "6/8");
  const instrumental = productionPreview({ ...draft, instrumental: true }, catalog);
  assert.equal(instrumental.bpm, "");
  assert.equal(instrumental.timeSignature, "");
  assert.equal(instrumental.stylePrompt, "warm piano");
});

test("full-version IPC accepts only trusted ids, bounded count, and optional text", () => {
  const valid = { songId: "a".repeat(20), versionId: `candidate_${"b".repeat(32)}`, versions: 2 };
  assert.deepEqual(parseRegenerateInput(valid), valid);
  for (const value of [null, { ...valid, strength: 0.7 },
    { ...valid, versions: 5 }, { ...valid, versions: 1.5 }, { ...valid, songId: "/tmp/song" },
    { ...valid, versionId: "/tmp/source.wav" }, { ...valid, lyrics: "" }, { ...valid, stylePrompt: 4 },
    { ...valid, referenceAudio: "/private/audio.wav" }, { ...valid, command: "run" }]) assert.throws(() => parseRegenerateInput(value));
});
