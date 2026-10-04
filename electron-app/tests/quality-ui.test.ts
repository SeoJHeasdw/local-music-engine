import assert from "node:assert/strict";
import { test } from "node:test";
import type { ActiveTask, AutomaticQuality, Song, TaskOutcome, Version } from "../shared.ts";
import { versionSourceParent, versionTree } from "../renderer/format.ts";
import { audioQualityText, isAutomaticAttempt, lyricQualityText, processingText, qualityLabel, TaskListeningFocus } from "../renderer/quality.ts";

const quality = (change: Partial<AutomaticQuality> = {}): AutomaticQuality => ({
  status: "passed", summary: "자동 검사 통과", groupId: "batch-slot", attempt: 1, maxAttempts: 4,
  originalSeed: 10, preferred: false, score: 98, retryReasons: [],
  audio: { automaticStatus: "passed" }, lyrics: { status: "pass", orderedCoverage: 1 }, processing: null, ...change,
});

function version(id: string, change: Partial<Version> = {}): Version {
  return { id, kind: "full", parentId: null, isFinal: false, recommended: false,
    review: { status: "unreviewed", rating: null, notes: [], updatedAt: null }, quality: quality(), ...change } as Version;
}

test("two requested winners remain two numbered roots and raw retries are retained as history", () => {
  const a1 = version("a1");
  const a2 = version("a2", { quality: quality({ attempt: 2 }) });
  const winnerA = version("A", { parentId: "a2", recommended: true,
    quality: quality({ attempt: 2, preferred: true, processing: { sourceCandidateId: "a2", gain: 0.9 } }) });
  const b1 = version("b1", { quality: quality({ groupId: "batch-slot-b", originalSeed: 11 }) });
  const winnerB = version("B", { parentId: "b1",
    quality: quality({ groupId: "batch-slot-b", originalSeed: 11, preferred: true, processing: { sourceCandidateId: "b1", gain: 1 } }) });
  const song = { versions: [a1, a2, winnerA, b1, winnerB], finalVersionId: null, recommendedVersionId: "A" } as Song;
  const tree = versionTree(song);
  const ready = tree.roots.filter((node) => !isAutomaticAttempt(node.version));
  assert.deepEqual(ready.map((node) => [node.version.id, node.name, node.depth]), [["A", "버전 1", 0], ["B", "버전 2", 0]]);
  assert.deepEqual(tree.flat.filter((node) => isAutomaticAttempt(node.version)).map((node) => node.version.id), ["a1", "a2", "b1"]);
  assert.match(tree.byId.get("a1")!.name, /버전 1.*시도 1/);
  assert.match(tree.byId.get("b1")!.name, /버전 2.*시도 1/);
  assert.equal(versionSourceParent(song, winnerA)?.id, "a2");
  assert.equal(winnerA.review.status, "unreviewed");
  assert.equal(winnerA.isFinal, false);
  assert.equal(song.finalVersionId, null);
});

test("a finished repaint follows its musical source rather than adding a raw-attempt nesting level", () => {
  const original = version("original", { quality: null });
  const raw = version("raw", { kind: "edit", parentId: "original" });
  const finished = version("finished", { kind: "edit", parentId: "raw",
    quality: quality({ preferred: true, processing: { sourceCandidateId: "raw" } }) });
  const song = { versions: [original, raw, finished] } as Song;
  const result = versionTree(song).byId.get("finished")!;
  assert.equal(result.name, "버전 1 › 수정 1");
  assert.equal(result.depth, 1);
  assert.equal(versionSourceParent(song, finished)?.id, "original");
});

test("unknown analysis is visibly unknown and conservative processing never claims repaired clipping", () => {
  const unknown = quality({ status: "unknown", audio: {}, lyrics: { status: "unknown", orderedCoverage: 1 } });
  assert.match(qualityLabel(unknown)!.label, /어려움/);
  assert.doesNotMatch(qualityLabel(unknown)!.label, /통과/);
  assert.match(lyricQualityText(unknown), /판단하지/);
  assert.match(audioQualityText(unknown), /확정하지/);
  const text = processingText({ gain: 0.8, endFadeSeconds: 0.005, channelDcOffsetsRemoved: [0.01] })!;
  assert.match(text, /음량을 낮췄어요/);
  assert.match(text, /원래 음원도 남아/);
  assert.doesNotMatch(text, /깨짐.*고쳤|클리핑.*수리/);
});

const task = { songId: "song", startedAt: 10 } as ActiveTask;
const outcome = { songId: "song", kind: "generate", ok: true, cancelled: false,
  newVersionIds: ["A", "B"], recommendedVersionId: "B" } as TaskOutcome;

test("recommendations focus automatically only while the listener has not changed song or version", () => {
  const unchanged = new TaskListeningFocus();
  unchanged.track(task, 5);
  unchanged.track({ ...task, progress: 0.8 }, 5);
  unchanged.track(null, 5);
  assert.equal(unchanged.complete(outcome, "song", 5), "B");
  const switched = new TaskListeningFocus();
  switched.track(task, 5);
  switched.track({ ...task, progress: 0.8 }, 7);
  assert.equal(switched.complete(outcome, "song", 7), null);
  const otherSong = new TaskListeningFocus();
  otherSong.track(task, 5);
  assert.equal(otherSong.complete(outcome, "another", 5), null);
  const reused = new TaskListeningFocus();
  reused.track(task, 5);
  assert.equal(reused.complete({ ...outcome, recommendedVersionId: "old" }, "song", 5), null);
});
