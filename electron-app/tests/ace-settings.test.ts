import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { ACE_DEFAULT_MODEL, ACE_LM_MODEL, defaultSettings, normalizeSettings } from "../main/settings-schema.ts";
import { loadAndMigrateSettings } from "../main/settings-storage.ts";
import { aceGenerationArgs, aceReady, aceRepaintArgs, requirePlanAction } from "../main/generation-arguments.ts";
import type { EngineStatus } from "../shared.ts";

const defaults = () => defaultSettings("/tmp/songs");

test("fresh preferences run ACE XL turbo with bounded five-minute generation", () => {
  const settings = defaults();
  assert.equal(settings.schemaVersion, 3);
  assert.equal(settings.engine, "ace-step");
  assert.equal(settings.musicModel, "acestep-v15-xl-turbo");
  assert.equal(settings.engineBaseUrl, "http://127.0.0.1:18001");
  assert.equal(settings.engineAutoStart, true);
  assert.equal(settings.defaultVersions, 1);
  assert.equal(settings.defaultDurationSeconds, 60);
  assert.equal(normalizeSettings({ ...settings, defaultDurationSeconds: 600 }, settings).defaultDurationSeconds, 300);
  for (const key of ["ditModel", "lmModel", "aceBaseUrl", "aceAutoStart", "repaintStrength"]) assert.equal(key in settings, false);
});

test("Music 3 preferences move to ACE XL on the ACE port and keep personal preferences", () => {
  const old = { schemaVersion: 2, engine: "minimax-music3", projectsDir: "/tmp/personal music", engineBaseUrl: "http://127.0.0.1:18002",
    engineAutoStart: false, musicModel: "mlx-community/MiniMax-Music3-bf16", defaultDurationSeconds: 240, defaultVersions: 4,
    feedbackStrength: "strong", assistant: { kind: "ollama", baseUrl: "http://127.0.0.1:11435", model: "local" }, lastExportDir: "/tmp/exports" };
  const snapshot = structuredClone(old);
  const result = normalizeSettings(old, defaults());
  assert.equal(result.engineBaseUrl, "http://127.0.0.1:18001");
  assert.equal(result.musicModel, ACE_DEFAULT_MODEL);
  assert.equal(result.engineAutoStart, false);
  assert.equal(result.projectsDir, old.projectsDir);
  assert.deepEqual(result.assistant, old.assistant);
  assert.equal(result.defaultVersions, 4);
  assert.equal(result.defaultDurationSeconds, 240);
  assert.equal(result.feedbackStrength, "strong");
  assert.equal(result.lastExportDir, old.lastExportDir);
  assert.deepEqual(old, snapshot);
});

test("first-generation ACE preferences keep their ACE address and start on XL", () => {
  const result = normalizeSettings({ aceBaseUrl: "http://127.0.0.1:18999", aceAutoStart: false, ditModel: "acestep-v15-turbo",
    repaintStrength: "light" }, defaults());
  assert.equal(result.engineBaseUrl, "http://127.0.0.1:18999");
  assert.equal(result.engineAutoStart, false);
  assert.equal(result.musicModel, ACE_DEFAULT_MODEL);
  assert.equal(result.feedbackStrength, "light");
});

test("current preferences keep the chosen listed DiT, reject remote URLs, and cannot inject a model", () => {
  const current = defaults();
  assert.equal(normalizeSettings({ ...current, engineBaseUrl: "http://localhost:18009" }, current).engineBaseUrl, "http://localhost:18009");
  assert.equal(normalizeSettings({ ...current, musicModel: "acestep-v15-turbo" }, current).musicModel, "acestep-v15-turbo");
  assert.equal(normalizeSettings({ ...current, musicModel: "acestep-v15-xl-sft" }, current).musicModel, current.musicModel);
  assert.throws(() => normalizeSettings({ ...current, engineBaseUrl: "http://example.com" }, current));
});

test("disk migration keeps the first backup per engine change and never touches project or audio data", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "ace-settings-"));
  try {
    const file = path.join(directory, "settings.json");
    const original = { schemaVersion: 2, engine: "minimax-music3", engineBaseUrl: "http://127.0.0.1:18002", projectsDir: directory, defaultDurationSeconds: 30 };
    await writeFile(file, JSON.stringify(original));
    await writeFile(path.join(directory, "project.json"), "original project");
    await writeFile(path.join(directory, "source.wav"), "original audio");
    const first = await loadAndMigrateSettings(file, defaults());
    assert.deepEqual(JSON.parse(await readFile(file, "utf8")), first);
    assert.equal(first.engine, "ace-step");
    assert.deepEqual(JSON.parse(await readFile(path.join(directory, "settings-before-ace-xl.json"), "utf8")), original);
    assert.equal(await readFile(path.join(directory, "project.json"), "utf8"), "original project");
    assert.equal(await readFile(path.join(directory, "source.wav"), "utf8"), "original audio");
    await writeFile(file, JSON.stringify({ ...original, defaultDurationSeconds: 60 }));
    await loadAndMigrateSettings(file, defaults());
    assert.deepEqual(JSON.parse(await readFile(path.join(directory, "settings-before-ace-xl.json"), "utf8")), original);
    const migrated = JSON.parse(await readFile(file, "utf8"));
    await loadAndMigrateSettings(file, defaults());
    assert.deepEqual(JSON.parse(await readFile(file, "utf8")), migrated);
    assert.equal((await readdir(directory)).some((name) => name.endsWith(".tmp")), false);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("generation and repaint arguments name the engine, the app's DiT and the 4B LM", () => {
  const settings = { ...defaults(), musicModel: "acestep-v15-turbo" as const };
  assert.deepEqual(aceGenerationArgs("/tmp/song", "41,42", settings), ["generate", "/tmp/song", "--seeds", "41,42", "--engine", "ace-step",
    "--base-url", "http://127.0.0.1:18001", "--model", "acestep-v15-turbo", "--lm-model", ACE_LM_MODEL]);
  assert.deepEqual(aceRepaintArgs("/tmp/song", "candidate_x", { startSeconds: 41, endSeconds: 58.256 }, "7", settings), ["repaint", "/tmp/song",
    "--engine", "ace-step", "--candidate-id", "candidate_x", "--start", "41.00", "--end", "58.26", "--seed", "7",
    "--model", "acestep-v15-turbo", "--base-url", "http://127.0.0.1:18001"]);
});

test("plans are either whole-song regeneration or a valid range repaint", () => {
  requirePlanAction({ action: "regenerate", range: null });
  requirePlanAction({ action: "repaint", range: { startSeconds: 2, endSeconds: 3 } });
  assert.throws(() => requirePlanAction({ action: "repaint", range: null }), /구간/);
  assert.throws(() => requirePlanAction({ action: "repaint", range: { startSeconds: 3, endSeconds: 3 } }), /구간/);
  assert.throws(() => requirePlanAction({ action: "repaint", range: { startSeconds: -1, endSeconds: 3 } }), /구간/);
  assert.throws(() => requirePlanAction({ action: "regenerate", range: { startSeconds: 2, endSeconds: 3 } }), /수정 방식/);
  const status = { engine: "ace-step", state: "ready", capabilities: { text2music: true } } as EngineStatus;
  assert.equal(aceReady(status), true);
  assert.equal(aceReady({ ...status, capabilities: { ...status.capabilities, text2music: false } }), false);
  assert.equal(aceReady({ ...status, state: "starting" }), false);
});
