import assert from "node:assert/strict";
import { test } from "node:test";
import { INSTRUMENTS, applyInstrumentChanges, hasInstrument } from "../renderer/instruments.ts";

const byTag = (tag: string) => INSTRUMENTS.find((item) => item.tag === tag)!;

test("instruments are found inside descriptive tags without confusing neighbours", () => {
  const style = "intimate male melodic rap, warm piano, mellow bass, 808 kick, electric piano chords";
  assert.equal(hasInstrument(style, byTag("piano")), true);
  assert.equal(hasInstrument("electric piano chords", byTag("piano")), false);
  assert.equal(hasInstrument(style, byTag("electric piano")), true);
  assert.equal(hasInstrument(style, byTag("groovy bassline")), true);
  assert.equal(hasInstrument("808 bass", byTag("groovy bassline")), false);
  assert.equal(hasInstrument(style, byTag("808 bass")), true);
  assert.equal(hasInstrument(style, byTag("electric guitar")), false);
});

test("removing drops every tag that names the instrument; adding appends its tag once", () => {
  const result = applyInstrumentChanges("hip hop, warm piano, piano chords, mellow bass", new Map([
    ["piano", "remove"],
    ["electric guitar", "add"],
    ["groovy bassline", "add"],
  ]));
  assert.equal(result.style, "hip hop, mellow bass, electric guitar");
  assert.deepEqual(result.changes, [
    { op: "remove", term: "warm piano", label: "피아노" },
    { op: "remove", term: "piano chords", label: "피아노" },
    { op: "add", term: "electric guitar", label: "일렉 기타" },
  ]);
});
