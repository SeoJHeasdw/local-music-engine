import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { defaultSettings, MUSIC3_MODEL, normalizeSettings } from "../main/settings-schema.ts";
import { loadAndMigrateSettings } from "../main/settings-storage.ts";
import { music3GenerationArgs, music3Ready, requireFullGeneration } from "../main/generation-arguments.ts";
import type { EngineStatus } from "../shared.ts";

const defaults = () => defaultSettings("/tmp/songs");

test("fresh preferences use only Music3 and bounded five-minute generation", () => {
  const settings = defaults();
  assert.equal(settings.schemaVersion, 2);
  assert.equal(settings.engine, "minimax-music3");
  assert.equal(settings.musicModel, MUSIC3_MODEL);
  assert.equal(settings.engineBaseUrl, "http://127.0.0.1:18002");
  assert.equal(settings.engineAutoStart, true);
  assert.equal(settings.defaultVersions, 1);
  assert.equal(settings.defaultDurationSeconds, 60);
  assert.equal(normalizeSettings({ defaultDurationSeconds: 600 }, settings).defaultDurationSeconds, 300);
  for (const key of ["ditModel", "lmModel", "aceBaseUrl", "aceAutoStart", "repaintStrength"]) assert.equal(key in settings, false);
});

test("migration replaces all legacy engine models and preserves personal preferences", () => {
  const old = { projectsDir: "/tmp/personal music", aceBaseUrl: "http://127.0.0.1:18999", aceAutoStart: false,
    ditModel: "custom-old-model", lmModel: "custom-old-lm", defaultDurationSeconds: 60, defaultVersions: 4,
    repaintStrength: "strong", assistant: { kind: "ollama", baseUrl: "http://127.0.0.1:11435", model: "local" }, lastExportDir: "/tmp/exports" };
  const snapshot = structuredClone(old);
  const result = normalizeSettings(old, defaults());
  assert.equal(result.engineBaseUrl, "http://127.0.0.1:18002");
  assert.equal(result.musicModel, MUSIC3_MODEL);
  assert.equal(result.engineAutoStart, false);
  assert.equal(result.projectsDir, old.projectsDir);
  assert.deepEqual(result.assistant, old.assistant);
  assert.equal(result.defaultVersions, 4);
  assert.equal(result.defaultDurationSeconds, 60);
  assert.equal(result.feedbackStrength, "strong");
  assert.equal(result.lastExportDir, old.lastExportDir);
  assert.deepEqual(old, snapshot);
});

test("versioned Music3 connection edits persist, reject remote URLs, and cannot inject a model", () => {
  const current = defaults();
  assert.equal(normalizeSettings({ ...current, engineBaseUrl: "http://localhost:18009" }, current).engineBaseUrl, "http://localhost:18009");
  assert.equal(normalizeSettings({ ...current, musicModel: "unrelated/model" }, current).musicModel, MUSIC3_MODEL);
  assert.throws(() => normalizeSettings({ ...current, engineBaseUrl: "http://example.com" }, current));
});

test("disk migration saves a separate first backup and never touches project or audio data", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "music3-settings-"));
  try {
    const file = path.join(directory, "settings.json");
    const original = { aceBaseUrl: "http://127.0.0.1:18001", ditModel: "acestep-v15-turbo", projectsDir: directory, defaultDurationSeconds: 30 };
    await writeFile(file, JSON.stringify(original));
    await writeFile(path.join(directory, "project.json"), "original project");
    await writeFile(path.join(directory, "source.wav"), "original audio");
    const first = await loadAndMigrateSettings(file, defaults());
    assert.deepEqual(JSON.parse(await readFile(file, "utf8")), first);
    assert.deepEqual(JSON.parse(await readFile(path.join(directory, "settings-before-music3.json"), "utf8")), original);
    assert.equal(await readFile(path.join(directory, "project.json"), "utf8"), "original project");
    assert.equal(await readFile(path.join(directory, "source.wav"), "utf8"), "original audio");
    await writeFile(file, JSON.stringify({ ...original, defaultDurationSeconds: 60 }));
    await loadAndMigrateSettings(file, defaults());
    assert.deepEqual(JSON.parse(await readFile(path.join(directory, "settings-before-music3.json"), "utf8")), original);
    assert.equal((await readdir(directory)).some((name) => name.endsWith(".tmp")), false);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test("generation arguments select Music3 explicitly and preserve seed text without legacy LM options", () => {
  const args = music3GenerationArgs("/tmp/song", "41,42", defaults());
  assert.deepEqual(args, ["generate", "/tmp/song", "--seeds", "41,42", "--engine", "minimax-music3", "--base-url", "http://127.0.0.1:18002", "--model", MUSIC3_MODEL]);
  assert.equal(args.includes("--lm-model"), false);
});

test("unsupported edits are rejected rather than silently converted to full generation", () => {
  requireFullGeneration({ action: "regenerate", range: null });
  assert.throws(() => requireFullGeneration({ action: "repaint", range: { startSeconds: 2, endSeconds: 3 } }), /구간 수정/);
  assert.throws(() => requireFullGeneration({ action: "regenerate", range: { startSeconds: 2, endSeconds: 3 } }), /구간 수정/);
  const status = { engine: "minimax-music3", state: "ready", capabilities: { text2music: true } } as EngineStatus;
  assert.equal(music3Ready(status), true);
  assert.equal(music3Ready({ ...status, capabilities: { ...status.capabilities, text2music: false } }), false);
  assert.equal(music3Ready({ ...status, state: "starting" }), false);
});
