import assert from "node:assert/strict";
import { test } from "node:test";
import { EngineManager, type EngineDependencies } from "../main/engine.ts";
import { modelLabel } from "../renderer/format.ts";

const ACE_OK = { status: "ok", service: "ACE-Step API", models_initialized: true, llm_initialized: true,
  loaded_model: "acestep-v15-xl-turbo", loaded_lm_model: "acestep-5Hz-lm-4B" };

function deferred<T = void>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

const answer = (data: unknown) => new Response(JSON.stringify({ data }));
const off = () => new Response("", { status: 503 });

function fixture(overrides: Partial<EngineDependencies> = {}, baseUrl = "http://127.0.0.1:18002") {
  const requests: { url: string; options?: RequestInit }[] = [];
  const dependencies: EngineDependencies = {
    fetch: async (input, options) => {
      requests.push({ url: String(input), options });
      return off();
    },
    installed: async () => {},
    authHeaders: async () => ({ Authorization: "Bearer test-only-key" }),
    pause: async () => {},
    ...overrides,
  };
  const engine = new EngineManager(() => baseUrl, () => {}, () => "acestep-v15-xl-turbo", dependencies);
  return { engine, requests, setUrl: (url: string) => { baseUrl = url; } };
}

test("an engine nobody runs is resting, not broken, and the app never starts it", async () => {
  const value = fixture();
  const status = await value.engine.check();
  assert.equal(status.state, "offline");
  assert.match(status.detail, /곡을 만들 때 켜고/);
  assert.equal("start" in value.engine, false);
  assert.equal("stop" in value.engine, false);
});

test("a missing ACE install is reported instead of a resting engine", async () => {
  const status = await fixture({ installed: async () => { throw new Error("ENOENT"); } }).engine.check();
  assert.equal(status.state, "missing");
  assert.match(status.detail, /bootstrap_ace\.sh/);
});

test("authentication is used for health without entering status", async () => {
  const value = fixture({ fetch: async (input, options) => {
    value.requests.push({ url: String(input), options });
    return answer(ACE_OK);
  } });
  await value.engine.check();
  assert.equal(value.requests.length, 1);
  assert.deepEqual(value.requests[0].options?.headers, { Authorization: "Bearer test-only-key" });
  assert.equal(value.requests[0].options?.redirect, "error");
  assert.equal(JSON.stringify(value.engine.snapshot()).includes("test-only-key"), false);
});

test("a loading ACE becomes ready only after model initialization", async () => {
  let loaded = false;
  const value = fixture({
    fetch: async () => answer({ ...ACE_OK, models_initialized: loaded, loaded_model: loaded ? ACE_OK.loaded_model : null, loaded_lm_model: null }),
  });
  assert.equal((await value.engine.check()).state, "starting");
  loaded = true;
  const ready = await value.engine.check();
  assert.equal(ready.state, "ready");
  assert.match(ready.detail, /저절로 꺼져요/);
  assert.equal(ready.maxDurationSeconds, 300);
});

test("the views name the DiT and LM the server reports as loaded, and a loaded ACE can repaint", async () => {
  const status = await fixture({ fetch: async () => answer(ACE_OK) }).engine.check();
  assert.deepEqual(status.models, { music: "acestep-v15-xl-turbo", lm: "acestep-5Hz-lm-4B" });
  assert.equal(modelLabel(status.models.music), "ACE-Step XL turbo");
  assert.equal(modelLabel("acestep-v15-turbo"), "ACE-Step turbo");
  assert.equal(modelLabel("another-dit"), "another-dit");
  assert.match(modelLabel(null), /엔진이 켜지면/);
  assert.deepEqual(status.capabilities, { text2music: true, cover: false, repaint: true, referenceAudio: false });
});

test("a model-loading failure is reported without the server's private error text", async () => {
  const status = await fixture({
    fetch: async () => answer({ status: "error", stage: "failed", service: "ACE-Step API", loaded_model: "acestep-v15-xl-turbo",
      loaded_lm_model: "acestep-5Hz-lm-4B", models_initialized: false, error: "Bearer secret-key in an internal exception" }),
  }).engine.check();
  assert.equal(status.state, "failed");
  assert.match(status.detail, /모델을 불러오지 못했어요/);
  assert.doesNotMatch(JSON.stringify(status), /secret-key|internal exception|Bearer/);
});

test("another engine, another DiT, or another LM never becomes ready", async () => {
  for (const data of [
    { engine: "minimax-music3", loaded_model: "mlx-community/MiniMax-Music3-bf16", loaded_lm_model: null },
    { service: "ACE-Step API", loaded_model: "acestep-v15-turbo", loaded_lm_model: "acestep-5Hz-lm-4B" },
    { service: "ACE-Step API", loaded_model: "acestep-v15-xl-turbo", loaded_lm_model: "acestep-5Hz-lm-1.7B" },
  ]) {
    const status = await fixture({ fetch: async () => answer({ status: "ok", models_initialized: true, ...data }) }).engine.check();
    assert.equal(status.state, "failed");
  }
});

test("a busy server that misses a poll stays on until it misses three", async () => {
  let up = true;
  const value = fixture({ fetch: async () => up ? answer(ACE_OK) : off() });
  assert.equal((await value.engine.check()).state, "ready");
  up = false;
  assert.equal((await value.engine.check()).state, "ready");
  assert.equal((await value.engine.check()).state, "ready");
  assert.equal((await value.engine.check()).state, "offline");
});

test("the assistant waits for the engine to stop itself and is refused while it keeps running", async () => {
  let checks = 0;
  const value = fixture({ fetch: async () => (checks++ < 2 ? answer(ACE_OK) : off()) });
  assert.equal(await value.engine.handoff.withEngineOff(async () => "answer"), "answer");
  const running = fixture({ fetch: async () => answer(ACE_OK) });
  await assert.rejects(running.engine.handoff.withEngineOff(async () => assert.fail("a running engine holds model memory")),
    /음악 엔진이 아직 메모리를 쓰고 있어서/);
});

test("a delayed poll for an old address cannot report the new address as ready", async () => {
  const pending = deferred<Response>();
  const value = fixture({ fetch: async (input) => String(input).includes(":18002/") ? pending.promise : off() });
  const oldPoll = value.engine.check();
  value.setUrl("http://127.0.0.1:18009");
  await value.engine.check();
  pending.resolve(answer(ACE_OK));
  await oldPoll;
  assert.equal(value.engine.snapshot().state, "offline");
  assert.equal(value.engine.snapshot().baseUrl, "http://127.0.0.1:18009");
});
