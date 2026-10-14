import assert from "node:assert/strict";
import { test } from "node:test";
import { MemoryHandoff } from "../main/handoff.ts";

// running: how many more checks find a music engine holding memory (Infinity: it never leaves).
function engine(running: number) {
  const events: string[] = [];
  return {
    events,
    running: async () => {
      events.push("check");
      return running-- > 0;
    },
    pause: async () => void events.push("wait"),
  };
}

test("with no music engine running the assistant is called at once", async () => {
  const fake = engine(0);
  const handoff = new MemoryHandoff(fake);
  const answer = await handoff.withEngineOff(async () => {
    fake.events.push("assistant");
    assert.equal(handoff.active(), true);
    return "draft";
  });
  assert.equal(answer, "draft");
  assert.deepEqual(fake.events, ["check", "assistant"]);
  assert.equal(handoff.active(), false);
});

test("the assistant waits for a server whose run just ended to stop itself", async () => {
  const fake = engine(2);
  await new MemoryHandoff(fake).withEngineOff(async () => void fake.events.push("assistant"));
  assert.deepEqual(fake.events, ["check", "wait", "check", "wait", "check", "assistant"]);
});

test("a music engine that keeps running is never shared with the assistant", async () => {
  const fake = engine(Infinity);
  let called = false;
  await assert.rejects(new MemoryHandoff(fake).withEngineOff(async () => {
    called = true;
  }), /음악 엔진이 아직 메모리를 쓰고 있어서/);
  assert.equal(called, false);
});

test("concurrent assistant calls queue instead of overlapping, and a generation start waits for them", async () => {
  const fake = engine(0);
  const handoff = new MemoryHandoff(fake);
  const order: string[] = [];
  let finishFirst!: () => void;
  const first = handoff.withEngineOff(() => new Promise<void>((resolve) => {
    order.push("first:begin");
    finishFirst = () => {
      order.push("first:end");
      resolve();
    };
  }));
  const second = handoff.withEngineOff(async () => void order.push("second"));
  const generation = handoff.withEngineStarting(async () => void order.push("generation starts"));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(order, ["first:begin"]);
  finishFirst();
  await Promise.all([first, second, generation]);
  assert.deepEqual(order, ["first:begin", "first:end", "second", "generation starts"]);
});
