import assert from "node:assert/strict";
import { test } from "node:test";
import { Player, type PlayerSource } from "../renderer/player.ts";

class AudioFixture extends EventTarget {
  preload = "";
  volume = 1;
  currentTime = 0;
  duration = NaN;
  paused = true;
  readyState = 0;
  private url = "";
  get src() { return this.url; }
  set src(value: string) {
    this.url = value;
    this.currentTime = 0;
    this.readyState = 0;
    this.duration = NaN;
  }
  removeAttribute(name: string) { if (name === "src") this.url = ""; }
  load() { this.currentTime = 0; }
  pause() { this.paused = true; this.dispatchEvent(new Event("pause")); }
  async play() { this.paused = false; this.dispatchEvent(new Event("play")); }
  ready(seconds: number) {
    this.duration = seconds;
    this.readyState = 4;
    this.dispatchEvent(new Event("loadedmetadata"));
  }
}

// Only browser audio/canvas boundaries are replaced. The load sequencing,
// playback intent, seeking, and looping run through the real player class.
const drawing = new Proxy({ measureText: () => ({ width: 20 }) }, {
  get: (object, key) => key in object ? object[key as keyof typeof object] : () => undefined,
  set: () => true,
});
Object.assign(globalThis, {
  Audio: AudioFixture,
  ResizeObserver: class { observe() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "", fontFamily: "system-ui" }),
  document: { documentElement: {}, body: {} },
  window: { devicePixelRatio: 1 },
  requestAnimationFrame: () => 1,
  cancelAnimationFrame: () => undefined,
});

function playerFixture(): { player: Player; audio: AudioFixture } {
  const canvas = {
    width: 800, height: 160,
    getContext: () => drawing,
    getBoundingClientRect: () => ({ width: 800, height: 160, left: 0 }),
    addEventListener: () => undefined,
  } as unknown as HTMLCanvasElement;
  const player = new Player(canvas);
  return { player, audio: player.audio as unknown as AudioFixture };
}

const source = (key: string, duration = 30): PlayerSource => ({ key, url: `music-audio://version/${key}`, peaks: [], duration });

test("rapid A/B switching preserves the original playhead and playing intent", async () => {
  const { player, audio } = playerFixture();
  const first = player.load(source("A"));
  audio.ready(30);
  await first;
  player.seek(12.5);
  player.toggle();
  assert.equal(player.playing, true);
  const second = player.load(source("B"));
  const third = player.load(source("C", 60));
  audio.ready(60);
  await Promise.all([second, third]);
  assert.equal(player.time, 12.5);
  assert.equal(player.playing, true);
  assert.equal(audio.src, "music-audio://version/C");
});

test("pausing while the comparison audio loads prevents delayed playback", async () => {
  const { player, audio } = playerFixture();
  const first = player.load(source("A"));
  audio.ready(30);
  await first;
  player.seek(8);
  player.toggle();
  const switching = player.load(source("B"));
  player.toggle();
  audio.ready(30);
  await switching;
  assert.equal(player.time, 8);
  assert.equal(player.playing, false);
});

test("a seek made during loading survives a subsequent comparison switch", async () => {
  const { player, audio } = playerFixture();
  const first = player.load(source("A", 60));
  audio.ready(60);
  await first;
  const second = player.load(source("B", 60));
  player.seek(20);
  const third = player.load(source("C", 30));
  audio.ready(30);
  await Promise.all([second, third]);
  assert.equal(player.time, 20);
});

test("a shorter comparison loops the overlapping interval and ignores a range beyond its end", async () => {
  const { player, audio } = playerFixture();
  const loading = player.load(source("A"));
  audio.ready(30);
  await loading;
  player.selection = { startSeconds: 24, endSeconds: 36 };
  player.loop = true;
  player.toggle();
  audio.currentTime = 29.99;
  audio.dispatchEvent(new Event("timeupdate"));
  assert.equal(player.time, 24);
  player.selection = { startSeconds: 35, endSeconds: 40 };
  audio.currentTime = 30;
  audio.dispatchEvent(new Event("timeupdate"));
  assert.equal(player.time, 30);
});

test("zooming keeps the pointed moment in place and never shows past the take", async () => {
  const { player, audio } = playerFixture();
  const loading = player.load(source("zoom", 120));
  audio.ready(120);
  await loading;
  assert.equal(player.zoom, 1);
  player.setZoom(4, 60);
  assert.equal(player.zoom, 4);
  assert.deepEqual(player.viewRange, { startSeconds: 45, endSeconds: 75 });
  player.zoomBy(2, 45);
  assert.deepEqual(player.viewRange, { startSeconds: 45, endSeconds: 60 });
  player.pan(-100);
  assert.deepEqual(player.viewRange, { startSeconds: 0, endSeconds: 15 });
  player.pan(1000);
  assert.deepEqual(player.viewRange, { startSeconds: 105, endSeconds: 120 });
  player.setZoom(1000);
  assert.equal(player.viewRange.endSeconds - player.viewRange.startSeconds, 3);
  player.showWhole();
  assert.deepEqual(player.viewRange, { startSeconds: 0, endSeconds: 120 });
});

test("fitting a selection frames it with room to drag its edges", async () => {
  const { player, audio } = playerFixture();
  const loading = player.load(source("fit", 180));
  audio.ready(180);
  await loading;
  player.setSelection({ startSeconds: 30, endSeconds: 40 });
  player.fitSelection();
  const view = player.viewRange;
  assert.ok(view.startSeconds < 30 && view.endSeconds > 40);
  assert.ok(Math.abs(view.endSeconds - view.startSeconds - 14) < 1e-9);
  // Seeking outside a zoomed view brings the playhead back on screen.
  player.seek(150);
  assert.ok(player.viewRange.startSeconds <= 150 && player.viewRange.endSeconds >= 150);
});
