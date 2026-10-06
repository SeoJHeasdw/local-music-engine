import assert from "node:assert/strict";
import { test } from "node:test";
import { canGoBack, canGoForward, record, resetHistory, step } from "../renderer/history.ts";

test("back and forward walk the visited screens like a browser", () => {
  resetHistory({ view: "library", songId: null });
  record({ view: "create", songId: null });
  record({ view: "studio", songId: "A" });
  assert.equal(canGoForward(), false);
  assert.deepEqual(step(-1), { view: "create", songId: null });
  assert.deepEqual(step(-1), { view: "library", songId: null });
  assert.equal(canGoBack(), false);
  assert.equal(step(-1), null);
  assert.deepEqual(step(1), { view: "create", songId: null });
  assert.equal(canGoForward(), true);
});

test("a new visit after going back drops the forward entries", () => {
  resetHistory({ view: "library", songId: null });
  record({ view: "studio", songId: "A" });
  record({ view: "studio", songId: "B" });
  step(-1);
  record({ view: "settings", songId: null });
  assert.equal(canGoForward(), false);
  assert.deepEqual(step(-1), { view: "studio", songId: "A" });
});

test("revisiting the same screen is not recorded twice, another song is", () => {
  resetHistory({ view: "studio", songId: "A" });
  record({ view: "studio", songId: "A" });
  assert.equal(canGoBack(), false);
  record({ view: "studio", songId: "B" });
  assert.deepEqual(step(-1), { view: "studio", songId: "A" });
});
