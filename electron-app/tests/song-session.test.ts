import assert from "node:assert/strict";
import { test } from "node:test";
import { SongSession } from "../main/song-session.ts";
import type { SongState } from "../shared.ts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

const state = (title: string) => ({ status: "ready", song: { title } } as SongState);

test("overlapping folder validation and load never reopen the older song", async () => {
  const session = new SongSession((folder) => folder);
  const slowA = deferred<string>();
  const slowReadB = deferred<SongState>();
  const reads: string[] = [];
  const persisted: string[] = [];
  const load = async (folder: string) => {
    reads.push(folder);
    return folder === "B" ? slowReadB.promise : state(folder);
  };
  const persist = async (folder: string) => { persisted.push(folder); };
  const openingA = session.open(() => slowA.promise, load, persist);
  const openingB = session.open(async () => "B", load, persist);
  await Promise.resolve();
  slowReadB.resolve(state("B"));
  assert.equal((await openingB)?.song?.title, "B");
  slowA.resolve("A");
  assert.equal(await openingA, null);
  assert.equal(session.current()?.folder, "B");
  assert.deepEqual(reads, ["B"]);
  assert.deepEqual(persisted, ["B"]);
});

test("a late disk read returns no state and cannot overwrite the later folder", async () => {
  const session = new SongSession((folder) => folder);
  const a = deferred<SongState>();
  const persisted: string[] = [];
  const load = async (folder: string) => folder === "A" ? a.promise : state(folder);
  const persist = async (folder: string) => { persisted.push(folder); };
  const openingA = session.open(async () => "A", load, persist);
  await Promise.resolve();
  const openingB = session.open(async () => "B", load, persist);
  await openingB;
  a.resolve(state("A"));
  assert.equal(await openingA, null);
  assert.equal(session.require("B").folder, "B");
  assert.throws(() => session.require("A"), /열린 곡이 바뀌었어요/);
  assert.deepEqual(persisted, ["B"]);
});

test("dialog and engine-await snapshots cannot mutate after a newer open, including reopening the same song", async () => {
  const session = new SongSession((folder) => folder);
  const open = (folder: string) => session.open(async () => folder, async () => state(folder), async () => undefined);
  await open("A");
  const dialog = session.require("A");
  const opening = session.beginOpen();
  assert.throws(() => session.require("A"), /열린 곡이 바뀌었어요/);
  session.cancelOpen(opening);
  assert.throws(() => session.assert(dialog), /열린 곡이 바뀌었어요/);
  const mutation = session.require("A");
  await open("B");
  await open("A");
  assert.equal(mutation.folder, "A");
  assert.throws(() => session.assert(mutation), /열린 곡이 바뀌었어요/);
});

test("closing a song invalidates an in-flight open and its background refresh", async () => {
  const session = new SongSession((folder) => folder);
  await session.open(async () => "A", async () => state("A"), async () => undefined);
  const refresh = session.require("A");
  const slow = deferred<SongState>();
  let persisted = false;
  const opening = session.open(async () => "B", () => slow.promise, async () => { persisted = true; });
  await Promise.resolve();
  session.close();
  slow.resolve(state("B"));
  assert.equal(await opening, null);
  assert.equal(session.matches(refresh), false);
  assert.equal(session.current(), null);
  assert.equal(persisted, false);
});

test("failed preference persistence restores the prior folder rather than leaving main and screen different", async () => {
  const session = new SongSession((folder) => folder);
  await session.open(async () => "A", async () => state("A"), async () => undefined);
  await assert.rejects(session.open(async () => "B", async () => state("B"), async () => {
    throw new Error("disk full");
  }), /disk full/);
  assert.equal(session.require("A").folder, "A");
  assert.throws(() => session.require("B"), /열린 곡이 바뀌었어요/);
});
