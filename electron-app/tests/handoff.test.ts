import assert from "node:assert/strict";
import { test } from "node:test";
import { MemoryHandoff, type Ownership } from "../main/handoff.ts";

function engine(ownership: Ownership) {
  const events: string[] = [];
  return {
    events,
    ownership: async () => ownership,
    stop: async () => void events.push("stop"),
    start: async () => void events.push("start"),
  };
}

test("an app-owned engine is stopped for the assistant call and restarted after", async () => {
  const fake = engine("owned");
  const handoff = new MemoryHandoff(fake);
  const answer = await handoff.withEngineStopped(async () => {
    fake.events.push("assistant");
    assert.equal(handoff.active(), true);
    return "draft";
  });
  assert.equal(answer, "draft");
  assert.deepEqual(fake.events, ["stop", "assistant", "start"]);
  assert.equal(handoff.active(), false);
});

test("the engine comes back even when the assistant call fails", async () => {
  const fake = engine("owned");
  await assert.rejects(new MemoryHandoff(fake).withEngineStopped(async () => {
    throw new Error("ollama down");
  }), /ollama down/);
  assert.deepEqual(fake.events, ["stop", "start"]);
});

test("an engine the app does not own is never stopped and the assistant is not called", async () => {
  const fake = engine("external");
  let called = false;
  await assert.rejects(new MemoryHandoff(fake).withEngineStopped(async () => {
    called = true;
  }), /앱 밖에서 켠 음악 엔진/);
  assert.equal(called, false);
  assert.deepEqual(fake.events, []);
});

test("with the engine off nothing is stopped or started", async () => {
  const fake = engine("off");
  await new MemoryHandoff(fake).withEngineStopped(async () => void fake.events.push("assistant"));
  assert.deepEqual(fake.events, ["assistant"]);
});

test("concurrent assistant calls queue instead of overlapping, and start waits for them", async () => {
  const fake = engine("off");
  const handoff = new MemoryHandoff(fake);
  let finishFirst!: () => void;
  const first = handoff.withEngineStopped(() => new Promise<void>((resolve) => {
    fake.events.push("first:begin");
    finishFirst = () => {
      fake.events.push("first:end");
      resolve();
    };
  }));
  const second = handoff.withEngineStopped(async () => void fake.events.push("second"));
  const engineStart = handoff.settled().then(() => void fake.events.push("engine may start"));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(fake.events, ["first:begin"]);
  finishFirst();
  await Promise.all([first, second, engineStart]);
  assert.deepEqual(fake.events, ["first:begin", "first:end", "second", "engine may start"]);
});
