import assert from "node:assert/strict";
import { test } from "node:test";
import type { AutomaticQuality, Song, Version } from "../shared.ts";
import { audibleSelection, comparisonChoices, comparisonVersion } from "../renderer/comparison.ts";
import { reviewCopy } from "../renderer/format.ts";

const version = (id: string, fields: Partial<Version> = {}): Version => ({
  id, kind: "full", parentId: null, fileOk: true, audioUrl: `music-audio://version/${id}`,
  review: { status: "unreviewed", rating: null, notes: [], updatedAt: null }, ...fields,
} as Version);
const quality = (fields: Partial<AutomaticQuality>): AutomaticQuality => ({
  status: "passed", summary: "자동 검사 통과", attempt: 1, maxAttempts: 1, originalSeed: 71,
  preferred: false, score: 98, retryReasons: [], audio: {}, lyrics: { status: "pass" }, processing: null, ...fields,
});

test("independent candidates can be compared without changing reviews, the final, or the recommendation", () => {
  const baseline = version("baseline", { review: { status: "approved", rating: 5, notes: [], updatedAt: null } });
  const raw = version("raw", { quality: quality({ preferred: false }) });
  const current = version("current", { parentId: "raw", quality: quality({ preferred: true, processing: { gain: 0.9 } }) });
  const missing = version("missing", { fileOk: false });
  const unusable = version("no-url", { audioUrl: null });
  const song = { versions: [baseline, raw, current, missing, unusable], finalVersionId: "baseline", recommendedVersionId: "current" } as Song;
  const before = JSON.stringify(song);
  assert.deepEqual(comparisonChoices(song, current).map((item) => item.id), ["baseline", "raw"]);
  assert.equal(comparisonVersion(song, current, "baseline"), baseline);
  assert.equal(comparisonVersion(song, current, null), raw);
  assert.equal(comparisonVersion(song, current, "missing"), undefined);
  assert.equal(comparisonVersion(song, baseline, "baseline"), undefined);
  assert.equal(comparisonVersion(song, current, "from-another-song"), undefined);
  assert.equal(JSON.stringify(song), before);
});

test("shorter comparison takes loop only a playable overlap", () => {
  assert.deepEqual(audibleSelection({ startSeconds: 24, endSeconds: 36 }, 30), { startSeconds: 24, endSeconds: 30 });
  assert.equal(audibleSelection({ startSeconds: 35, endSeconds: 40 }, 30), null);
  assert.equal(audibleSelection({ startSeconds: 30, endSeconds: 40 }, 30), null);
  assert.equal(audibleSelection({ startSeconds: 29.99, endSeconds: 40 }, 30), null);
  assert.equal(audibleSelection({ startSeconds: 10, endSeconds: 5 }, 30), null);
  assert.equal(audibleSelection({ startSeconds: 0, endSeconds: Infinity }, 30)?.endSeconds, 30);
  assert.equal(audibleSelection({ startSeconds: NaN, endSeconds: 5 }, 30), null);
  assert.equal(audibleSelection(null, 30), null);
});

test("listened records listening without inventing an ambiguous music verdict", () => {
  assert.equal(reviewCopy.listened.label, "들어 봤어요");
  assert.equal(reviewCopy.approved.label, "좋아요");
});
