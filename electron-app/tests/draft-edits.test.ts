import assert from "node:assert/strict";
import { test } from "node:test";
import { DraftEdits } from "../renderer/draft-edits.ts";
import type { CreateDraft } from "../renderer/store.ts";
import type { DraftResult } from "../shared.ts";

const draft = (): CreateDraft => ({
  description: "잔잔한 노래", instrumental: false, title: "", stylePrompt: "", lyrics: "",
  durationSeconds: 120, versions: 2, bpm: "", keyScale: "", timeSignature: "", drafted: false,
});
const result: DraftResult = {
  title: "초안 제목", stylePrompt: "calm, piano", lyrics: "초안 가사", durationSeconds: 120,
  bpm: 90, keyScale: "C major", timeSignature: "4/4", source: "llm", sourceModel: "local", notes: [],
};

test("a delayed draft preserves every field the person edited while waiting", () => {
  const edits = new DraftEdits();
  const initial = draft();
  const ticket = edits.begin(initial);
  const human = { title: "내 제목", lyrics: "내 가사", stylePrompt: "my style", bpm: "108" };
  edits.edited(human);
  const current = { ...initial, ...human };
  const patch = edits.merge(ticket, current, result, "새 곡");
  assert.deepEqual({ ...current, ...patch }, { ...current, drafted: true });
});

test("manual edits to a previous AI draft survive the next draft too", () => {
  const edits = new DraftEdits();
  const initial = draft();
  const first = edits.merge(edits.begin(initial), initial, result, "새 곡");
  let current = { ...initial, ...first };
  edits.edited({ lyrics: "직접 수정한 가사" });
  current = { ...current, lyrics: "직접 수정한 가사" };
  const second = edits.merge(edits.begin(current), current, { ...result, title: "새 제목", lyrics: "새 초안 가사" }, "새 곡");
  assert.equal(second?.title, "새 제목");
  assert.equal(second?.lyrics, undefined);
});

test("existing human style, title and lyrics are preserved on the first draft", () => {
  const edits = new DraftEdits();
  const initial = { ...draft(), title: "사람 제목", stylePrompt: "warm", lyrics: "사람 가사" };
  const patch = edits.merge(edits.begin(initial), initial, result, "새 곡");
  assert.equal(patch?.title, undefined);
  assert.equal(patch?.stylePrompt, undefined);
  assert.equal(patch?.lyrics, undefined);
});

test("input changes and a newer request discard late drafts even if the values are restored", () => {
  const edits = new DraftEdits();
  const initial = draft();
  const old = edits.begin(initial);
  edits.edited({ instrumental: true });
  edits.edited({ instrumental: false });
  assert.equal(edits.merge(old, initial, result, "새 곡"), null);
  const newer = edits.begin(initial);
  assert.equal(edits.merge(old, initial, result, "새 곡"), null);
  edits.reset();
  assert.equal(edits.merge(newer, initial, result, "새 곡"), null);
});

test("changing a production checkbox discards a late draft even after restoring the old rules", () => {
  const edits = new DraftEdits();
  const initial = { ...draft(), productionRules: { version: 1 as const, presetId: null, ruleIds: [] } };
  const ticket = edits.begin(initial);
  edits.edited({ productionRules: { version: 1, presetId: null, ruleIds: ["steady-groove"] } });
  edits.edited({ productionRules: initial.productionRules });
  assert.equal(edits.merge(ticket, initial, result, "새 곡"), null);
});

test("language belongs to the draft request and a language change discards its late response", () => {
  const edits = new DraftEdits();
  const initial = { ...draft(), vocalLanguage: "ko" as const };
  const ticket = edits.begin(initial);
  edits.edited({ vocalLanguage: "en" });
  assert.equal(edits.merge(ticket, { ...initial, vocalLanguage: "en" }, result, "새 곡"), null);
});
