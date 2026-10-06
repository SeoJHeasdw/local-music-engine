import assert from "node:assert/strict";
import { test } from "node:test";
import { parseWindowState, windowBounds } from "../main/window-state.ts";

const primary = { x: 0, y: 25, width: 1728, height: 1092 };

test("a first launch fills the primary work area", () => {
  assert.deepEqual(windowBounds(null, [primary], primary), primary);
});

test("saved bounds are reused only while they are on a connected display", () => {
  const saved = { x: 100, y: 80, width: 1400, height: 900 };
  assert.deepEqual(windowBounds(saved, [primary], primary), saved);
  // The external monitor it lived on is gone.
  assert.deepEqual(windowBounds({ x: 3000, y: 0, width: 1400, height: 900 }, [primary], primary), primary);
  // Too small to be usable.
  assert.deepEqual(windowBounds({ x: 0, y: 0, width: 400, height: 300 }, [primary], primary), primary);
});

test("only well-formed saved state is trusted", () => {
  assert.equal(parseWindowState(null), null);
  assert.equal(parseWindowState({ bounds: { x: 0, y: 0, width: "wide", height: 900 } }), null);
  assert.deepEqual(parseWindowState({ bounds: { x: 1.4, y: 2, width: 1400, height: 900 }, fullscreen: true, maximized: "yes" }),
    { bounds: { x: 1, y: 2, width: 1400, height: 900 }, fullscreen: true, maximized: false });
});
