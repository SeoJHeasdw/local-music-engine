import assert from "node:assert/strict";
import { test } from "node:test";
import { sectionEnergyLabel, sectionGuidanceCopy } from "../renderer/song-plan-copy.ts";

test("section guidance explains the musical intent in Korean without claiming measured loudness", () => {
  assert.equal(sectionEnergyLabel(0.25), "차분하게");
  assert.equal(sectionEnergyLabel(0.44), "담백하게");
  assert.equal(sectionEnergyLabel(0.62), "조금씩 고조");
  assert.equal(sectionEnergyLabel(0.9), "힘 있게");
  assert.match(sectionGuidanceCopy("Verse", "restrained groove, space for the main phrase", false), /목소리.*공간/);
  assert.match(sectionGuidanceCopy("Chorus", "fuller groove, clear main hook", false), /후렴.*편곡/);
  assert.match(sectionGuidanceCopy("Theme reprise", "fuller groove, clear main hook", true), /주요 선율/);
  assert.match(sectionGuidanceCopy("Pre-Chorus", "gradual lift, steady groove", false), /박자.*고조/);
  assert.match(sectionGuidanceCopy("Intro", "light main instrument, brief entrance", false), /가볍게 시작/);
  assert.match(sectionGuidanceCopy("Outro", "gentle resolved ending", false), /마무리/);
});
