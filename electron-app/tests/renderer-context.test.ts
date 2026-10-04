import assert from "node:assert/strict";
import { test } from "node:test";
import { get, initStore, isPlanTicket, listeningRevision, planTicket, set, songTicket, type State } from "../renderer/store.ts";
import type { MusicAppApi, SongState } from "../shared.ts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

const song = (songId: string): SongState => ({
  status: "ready", song: { songId, title: songId, versions: [], finalVersionId: null },
} as unknown as SongState);

function initial(): State {
  return {
    view: "library", song: song("previous"), activeVersionId: "candidate_A", plan: null, planning: false,
    settings: { assistant: { kind: "rules" } }, feedback: "발음 수정", scope: "song", strength: "medium", planVersions: 2,
    selection: null, listenToParent: false, comparisonVersionId: null,
  } as State;
}

// Exercise the actual renderer action functions with delayed IPC promises. They do
// not need a browser because this test only uses song switching and state application.
const pending = new Map<string, ReturnType<typeof deferred<SongState | null>>>();
let refresh = deferred<SongState>();
const api = {
  openSong: (id: string) => pending.get(id)!.promise,
  refreshSong: () => refresh.promise,
} as unknown as MusicAppApi;
const globals = globalThis as unknown as { window: { musicApp: MusicAppApi }; document: { getElementById: () => null } };
globals.window = { musicApp: api };
globals.document = { getElementById: () => null };
const actions = await import("../renderer/actions.ts");

test("the actual open action ignores A when B finishes first", async () => {
  initStore(initial());
  const a = deferred<SongState | null>();
  const b = deferred<SongState | null>();
  pending.set("A", a);
  pending.set("B", b);
  const openA = actions.openSong("A");
  const openB = actions.openSong("B");
  b.resolve(song("B"));
  await openB;
  a.resolve(song("A"));
  await openA;
  assert.equal(get().song.song?.songId, "B");
  assert.equal(get().view, "studio");
});

test("task-finished/manual refresh and mutation replies cannot return the listener to an older song", async () => {
  initStore({ ...initial(), song: song("A") });
  const mutation = songTicket("A");
  refresh = deferred<SongState>();
  const refreshing = actions.refreshSong("A", "late-edit");
  const b = deferred<SongState | null>();
  pending.set("B", b);
  const opening = actions.openSong("B");
  b.resolve(song("B"));
  await opening;
  refresh.resolve(song("A"));
  await refreshing;
  assert.equal(actions.applySongForContext(song("A"), mutation), false);
  assert.equal(get().song.song?.songId, "B");
  assert.notEqual(get().activeVersionId, "late-edit");
});

test("pending plans are invalidated by version and input changes even when values are restored", () => {
  initStore({ ...initial(), song: song("A") });
  const versionRequest = planTicket();
  set({ activeVersionId: "candidate_B" });
  set({ activeVersionId: "candidate_A" });
  assert.equal(isPlanTicket(versionRequest), false);
  const inputRequest = planTicket();
  set({ feedback: "새 요청" });
  set({ feedback: "발음 수정" });
  assert.equal(isPlanTicket(inputRequest), false);
  const rangeRequest = planTicket();
  set({ selection: { startSeconds: 5, endSeconds: 10 } });
  assert.equal(isPlanTicket(rangeRequest), false);
});

test("comparison changes count as manual listening and are reset when another song opens", () => {
  initStore({ ...initial(), song: song("A") });
  const revision = listeningRevision();
  set({ comparisonVersionId: "baseline" });
  assert.ok(listeningRevision() > revision);
  actions.applySong(song("A"));
  assert.equal(get().comparisonVersionId, "baseline");
  actions.applySong(song("B"));
  assert.equal(get().comparisonVersionId, null);
  assert.equal(get().listenToParent, false);
});
