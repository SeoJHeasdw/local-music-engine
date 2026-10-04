import assert from "node:assert/strict";
import { test } from "node:test";
import type { ProductionCatalog } from "../shared.ts";
import { emptyProductionRules, parseProductionBpm, parseProductionRules, productionPreview, selectProductionPreset, toggleProductionRule } from "../production-rules.ts";

const catalog: ProductionCatalog = {
  version: 1,
  presets: [{ id: "emotional-hiphop", label: "감성 힙합", description: "느긋한 리듬", caption: "emotional hip hop", bpm: 84, keyScale: "A minor", timeSignature: "4", ruleIds: ["steady-groove", "clear-vocal", "simple-structure"] }],
  rules: [
    { id: "steady-groove", label: "일정한 리듬", description: "", caption: "steady groove" },
    { id: "clear-vocal", label: "또렷한 보컬", description: "", caption: "clear vocal", instrumentalCaption: "" },
    { id: "simple-structure", label: "단순한 구조", description: "", caption: "verse chorus structure", shortCaption: "short verse then hook", instrumentalCaption: "theme and variation", shortInstrumentalCaption: "short repeated theme" },
    { id: "clean-production", label: "깔끔한 음질", description: "", caption: "clean mix" },
  ],
};
const input = () => ({ stylePrompt: "", instrumental: false, durationSeconds: 30, bpm: "", keyScale: "", timeSignature: "", productionRules: emptyProductionRules() });

test("main parses IPC selections against the catalog before side effects", () => {
  assert.deepEqual(parseProductionRules(undefined, catalog), emptyProductionRules());
  assert.deepEqual(parseProductionRules(selectProductionPreset("emotional-hiphop", catalog), catalog), selectProductionPreset("emotional-hiphop", catalog));
  for (const value of [null, [], "emotional-hiphop", { version: "1", presetId: null, ruleIds: [] },
    { version: 1, presetId: "unknown", ruleIds: [] }, { version: 1, presetId: null, ruleIds: ["unknown"] },
    { version: 1, presetId: null, ruleIds: ["steady-groove", "steady-groove"] },
    { version: 1, presetId: null, ruleIds: [1] }, { version: 1, presetId: null, ruleIds: [], command: "arbitrary" }]) {
    assert.throws(() => parseProductionRules(value, catalog));
  }
});

test("genre fallback metadata preserves explicit user values and leaves editable input untouched", () => {
  const draft = { ...input(), stylePrompt: "my acoustic idea", bpm: "97", keyScale: "D major", timeSignature: "3/4", productionRules: selectProductionPreset("emotional-hiphop", catalog) };
  const before = structuredClone(draft);
  const preview = productionPreview(draft, catalog);
  assert.equal(preview.bpm, "97");
  assert.equal(preview.keyScale, "D major");
  assert.equal(preview.timeSignature, "3/4");
  assert.match(preview.stylePrompt, /^my acoustic idea, emotional hip hop,/);
  assert.deepEqual(draft, before);
  const fallback = productionPreview({ ...input(), productionRules: selectProductionPreset("emotional-hiphop", catalog) }, catalog);
  assert.equal(fallback.bpm, "84");
  assert.equal(fallback.keyScale, "A minor");
  assert.equal(fallback.timeSignature, "4/4");
});

test("checkbox preview uses selected catalog order, short captions, and skips vocal rules for instrumental", () => {
  const selection = { version: 1 as const, presetId: null, ruleIds: ["simple-structure", "clear-vocal", "steady-groove"] };
  const sung = productionPreview({ ...input(), productionRules: selection }, catalog);
  assert.equal(sung.stylePrompt, "steady groove, clear vocal, short verse then hook");
  const instrumental = productionPreview({ ...input(), instrumental: true, productionRules: selection }, catalog);
  assert.equal(instrumental.stylePrompt, "steady groove, short repeated theme");
  assert.deepEqual(instrumental.captions.map((rule) => rule.id), ["steady-groove", "simple-structure"]);
  assert.equal(productionPreview({ ...input(), durationSeconds: 46, productionRules: selection }, catalog).stylePrompt, "steady groove, clear vocal, verse chorus structure");
  assert.deepEqual(selection.ruleIds, ["simple-structure", "clear-vocal", "steady-groove"]);
});

test("recommendation choices can be individually unchecked and clearing preset clears all checks", () => {
  const selected = selectProductionPreset("emotional-hiphop", catalog);
  const edited = toggleProductionRule(selected, "steady-groove", false);
  assert.equal(edited.presetId, "emotional-hiphop");
  assert.deepEqual(edited.ruleIds, ["clear-vocal", "simple-structure"]);
  assert.equal(productionPreview({ ...input(), productionRules: { ...edited, ruleIds: [] } }, catalog).stylePrompt, "emotional hip hop");
  assert.deepEqual(selectProductionPreset(null, catalog), emptyProductionRules());
  assert.deepEqual(selected.ruleIds, catalog.presets[0].ruleIds);
});

test("manual conflicting texture remains visible with a guidance warning", () => {
  const preview = productionPreview({ ...input(), stylePrompt: "lo-fi, vinyl crackle", productionRules: { version: 1, presetId: null, ruleIds: ["clean-production"] } }, catalog);
  assert.equal(preview.stylePrompt, "lo-fi, vinyl crackle, clean mix");
  assert.equal(preview.warnings.length, 1);
});

test("invalid manually entered tempo is rejected instead of silently replaced by a preset default", () => {
  assert.equal(parseProductionBpm(" 97 "), 97);
  assert.equal(parseProductionBpm(""), null);
  for (const value of ["97abc", "97.4", "0", "301", "1e2", "0x54"]) assert.throws(() => parseProductionBpm(value));
});

test("a vocal rule alone cannot provide an instrumental song's effective style", () => {
  assert.equal(productionPreview({ ...input(), instrumental: true, productionRules: { version: 1, presetId: null, ruleIds: ["clear-vocal"] } }, catalog).stylePrompt, "");
});

test("detailed Music3 guidance does not inherit the legacy word or short-caption limit", () => {
  const stylePrompt = "warm acoustic melody ".repeat(40).trim();
  assert.ok(stylePrompt.length > 650);
  assert.ok(stylePrompt.split(/\s+/).length > 100);
  const detailedCatalog = { ...catalog, captionBudgetCharacters: 16_000, captionBudgetScope: "api_prompt_character_limit" as const, captionTokenBudgetMeasured: false };
  const preview = productionPreview({ ...input(), stylePrompt }, detailedCatalog);
  assert.equal(preview.stylePrompt, stylePrompt);
  assert.deepEqual(preview.warnings, []);
  assert.deepEqual(productionPreview({ ...input(), stylePrompt }, catalog).warnings, [], "the fallback is also Music3's character bound");
});

test("guidance beyond the published character bound shows an advisory and preserves the original text", () => {
  const stylePrompt = "warm acoustic melody ".repeat(800).trim();
  const draft = { ...input(), stylePrompt };
  const before = structuredClone(draft);
  const preview = productionPreview(draft, { ...catalog, captionBudgetCharacters: 16_000 });
  assert.equal(preview.stylePrompt, stylePrompt);
  assert.match(preview.warnings[0], /제작 안내가 길어요/);
  assert.equal(preview.warnings.some((message) => /전달되지|토큰/.test(message)), false);
  assert.deepEqual(draft, before);
});

test("preview matches backend suppression of already present full guidance fragments, case-insensitively", () => {
  const selection = selectProductionPreset("emotional-hiphop", catalog);
  const first = productionPreview({ ...input(), productionRules: selection }, catalog);
  const second = productionPreview({ ...input(), stylePrompt: first.stylePrompt.toUpperCase(), productionRules: selection }, catalog);
  assert.equal(second.stylePrompt, first.stylePrompt.toUpperCase());
  const presetOnly = productionPreview({ ...input(), stylePrompt: "EMOTIONAL HIP HOP", productionRules: selection }, catalog);
  assert.equal(presetOnly.stylePrompt, "EMOTIONAL HIP HOP, steady groove, clear vocal, short verse then hook");
});
