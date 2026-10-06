import assert from "node:assert/strict";
import type { ChildProcess, SpawnOptions } from "node:child_process";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import { aceLaunchAddress, EngineManager, type EngineDependencies } from "../main/engine.ts";
import { modelLabel } from "../renderer/format.ts";

const ACE_OK = { status: "ok", service: "ACE-Step API", models_initialized: true, llm_initialized: true,
  loaded_model: "acestep-v15-xl-turbo", loaded_lm_model: "acestep-5Hz-lm-4B" };

function deferred<T = void>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function drainUntil(check: () => boolean): Promise<void> {
  for (let index = 0; index < 30 && !check(); index += 1) {
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
  assert.equal(check(), true, "the queued operation did not complete");
}

function fixture(
  baseUrl = "http://127.0.0.1:18002",
  beforeStart: () => Promise<void> = async () => {},
  overrides: Partial<EngineDependencies> = {},
) {
  const events: string[] = [];
  const requests: { url: string; options?: RequestInit }[] = [];
  const launches: SpawnOptions[] = [];
  let live: ChildProcess | null = null;
  let external = false;
  const dependencies: EngineDependencies = {
    fetch: async (input, options) => {
      requests.push({ url: String(input), options });
      return external || live
        ? new Response(JSON.stringify({ data: ACE_OK }))
        : new Response("", { status: 503 });
    },
    installed: async () => {},
    logFile: () => "/unused/ace-server.log",
    openLog: async () => ({ write: () => {}, end: () => {} }),
    spawn: (_command, _args, options) => {
      events.push("spawn");
      launches.push(options);
      const child = Object.assign(new EventEmitter(), {
        pid: 200_000 + launches.length,
        stdout: new PassThrough(),
        stderr: new PassThrough(),
      }) as unknown as ChildProcess;
      live = child;
      child.once("exit", () => { if (live === child) live = null; });
      return child;
    },
    kill: (child) => {
      events.push("stop");
      assert.equal(live, child);
      live = null;
      queueMicrotask(() => child.emit("exit", 0, "SIGTERM"));
    },
    pause: async () => {},
    ensureApiKey: async () => { events.push("key"); },
    authHeaders: async () => ({ Authorization: "Bearer test-only-key" }),
  };
  Object.assign(dependencies, overrides);
  const engine = new EngineManager(() => baseUrl, () => {}, () => "acestep-v15-xl-turbo", beforeStart, dependencies);
  return {
    engine, events, requests, launches, dependencies,
    live: () => Boolean(live),
    external: () => { external = true; },
    setUrl: (url: string) => { baseUrl = url; },
    close: async () => { if (live) await engine.stop(); },
  };
}

test("startup holds memory while preparing and an arriving assistant stops ACE before use", async () => {
  const preparing = deferred();
  const releasePreparation = deferred();
  let preparationCalls = 0;
  const value = fixture("http://127.0.0.1:18002", async () => {
    if (++preparationCalls === 1) {
      preparing.resolve();
      await releasePreparation.promise;
    }
  });
  const start = value.engine.start();
  assert.equal(value.engine.start(), start, "concurrent starts should share one operation");
  await preparing.promise;
  let assistantRan = false;
  const answer = value.engine.handoff.withEngineStopped(async () => {
    assert.equal(value.live(), false, "the assistant must never share memory with ACE");
    assistantRan = true;
    value.events.push("assistant");
    return "draft";
  });
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(assistantRan, false);
  assert.deepEqual(value.events, []);
  releasePreparation.resolve();
  await start;
  assert.equal(await answer, "draft");
  await drainUntil(() => value.launches.length === 2);
  assert.deepEqual(value.events, ["key", "spawn", "stop", "assistant", "key", "spawn"]);
  assert.equal(value.engine.handoff.active(), false);
  await value.close();
});

test("a user start waits for an assistant and its automatic restart without deadlock", async () => {
  const value = fixture();
  await value.engine.start();
  const entered = deferred();
  const finish = deferred();
  const answer = value.engine.handoff.withEngineStopped(async () => {
    assert.equal(value.live(), false);
    entered.resolve();
    await finish.promise;
    return "answer";
  });
  await entered.promise;
  const pendingStart = value.engine.start();
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(value.launches.length, 1);
  finish.resolve();
  assert.equal(await answer, "answer");
  await pendingStart;
  assert.equal(value.launches.length, 2, "restart and explicit start must coalesce");
  await value.close();
});

test("failing preparation releases memory for the assistant and does not spawn ACE", async () => {
  const value = fixture("http://127.0.0.1:18002", async () => { throw new Error("assistant unload failed"); });
  assert.equal((await value.engine.start()).state, "failed");
  const result = await value.engine.handoff.withEngineStopped(async () => {
    assert.equal(value.live(), false);
    return "answer";
  });
  assert.equal(result, "answer");
  assert.equal(value.launches.length, 0);
});

test("stop cancels pending preparation and its completion cannot clear a newer start", async () => {
  const firstEntered = deferred();
  const finishFirst = deferred();
  const secondEntered = deferred();
  const finishSecond = deferred();
  let preparations = 0;
  const value = fixture("http://127.0.0.1:18002", async () => {
    if (++preparations === 1) {
      firstEntered.resolve();
      await finishFirst.promise;
    } else {
      secondEntered.resolve();
      await finishSecond.promise;
    }
  });
  const firstStart = value.engine.start();
  await firstEntered.promise;
  await value.engine.stop();
  const secondStart = value.engine.start();
  assert.notEqual(secondStart, firstStart);
  finishFirst.resolve();
  await firstStart;
  await secondEntered.promise;
  assert.equal(value.engine.start(), secondStart);
  assert.deepEqual(value.events, []);
  finishSecond.resolve();
  await secondStart;
  assert.equal(value.launches.length, 1);
  await value.close();
});

test("an explicit restart waits for the old process to exit before starting another", async () => {
  const exit = deferred();
  const value = fixture("http://127.0.0.1:18002", async () => {}, {
    kill: (child) => { void exit.promise.then(() => child.emit("exit", 0, "SIGTERM")); },
  });
  await value.engine.start();
  const stopping = value.engine.stop();
  const restarting = value.engine.start();
  let finished = false;
  void restarting.then(() => { finished = true; });
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(finished, false);
  assert.equal(value.launches.length, 1);
  exit.resolve();
  await stopping;
  await restarting;
  assert.equal(value.launches.length, 2);
  assert.equal(value.live(), true);
  await value.close();
});

test("quitting during startup preparation prevents any later spawn", async () => {
  const entered = deferred();
  const finish = deferred();
  const value = fixture("http://127.0.0.1:18002", async () => {
    entered.resolve();
    await finish.promise;
  });
  const pending = value.engine.start();
  await entered.promise;
  value.engine.disposeOwned();
  finish.resolve();
  await pending;
  await value.engine.start();
  assert.deepEqual(value.events, []);
});

test("quitting during log preparation closes the unused stream and does not spawn", async () => {
  const entered = deferred();
  const finish = deferred();
  let closed = false;
  const value = fixture("http://127.0.0.1:18002", async () => {}, {
    openLog: async () => {
      entered.resolve();
      await finish.promise;
      return { write: () => {}, end: () => { closed = true; } };
    },
  });
  const pending = value.engine.start();
  await entered.promise;
  value.engine.disposeOwned();
  finish.resolve();
  await pending;
  assert.equal(closed, true);
  assert.equal(value.launches.length, 0);
});

test("an explicit stop during an assistant call suppresses automatic restart", async () => {
  const value = fixture();
  await value.engine.start();
  const entered = deferred();
  const finish = deferred();
  const answer = value.engine.handoff.withEngineStopped(async () => {
    entered.resolve();
    await finish.promise;
  });
  await entered.promise;
  await value.engine.stop();
  finish.resolve();
  await answer;
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(value.launches.length, 1);
  assert.equal(value.live(), false);
});

test("quit during an assistant cancels queued explicit start and automatic restart", async () => {
  const value = fixture();
  await value.engine.start();
  const entered = deferred();
  const finish = deferred();
  const answer = value.engine.handoff.withEngineStopped(async () => {
    entered.resolve();
    await finish.promise;
  });
  await entered.promise;
  const pending = value.engine.start();
  value.engine.disposeOwned();
  finish.resolve();
  await answer;
  await pending;
  await value.engine.start();
  assert.equal(value.launches.length, 1);
  assert.equal(value.live(), false);
});

test("authentication is prepared before spawn and used for health without entering status", async () => {
  const value = fixture();
  await value.engine.start();
  await value.engine.check();
  assert.deepEqual(value.events, ["key", "spawn"]);
  assert.ok(value.requests.length >= 2);
  for (const request of value.requests) {
    assert.deepEqual(request.options?.headers, { Authorization: "Bearer test-only-key" });
    assert.equal(request.options?.redirect, "error");
  }
  assert.equal(JSON.stringify(value.engine.snapshot()).includes("test-only-key"), false);
  await value.close();
});

test("a normalized explicit HTTP port 80 launches and polls that same port", async () => {
  const baseUrl = new URL("http://127.0.0.1:80").origin;
  assert.equal(baseUrl, "http://127.0.0.1");
  const value = fixture(baseUrl);
  await value.engine.start();
  assert.equal(value.launches[0].env?.MUSIC_ENGINE_ACE_PORT, "80");
  assert.equal(value.launches[0].env?.MUSIC_ENGINE_ACE_HOST, "127.0.0.1");
  assert.equal(value.requests[0].url, "http://127.0.0.1/health");
  await value.close();
});

test("launch bindings follow validated loopback hosts and ports", () => {
  assert.deepEqual(aceLaunchAddress("http://localhost:18002"), { host: "localhost", port: "18002" });
  assert.deepEqual(aceLaunchAddress("http://[::1]:18003/"), { host: "::1", port: "18003" });
  assert.throws(() => aceLaunchAddress("http://127.0.0.1:18002/prefix"), /경로/);
  assert.throws(() => aceLaunchAddress("https://127.0.0.1:18002"), /이 Mac/);
  assert.throws(() => aceLaunchAddress("http://example.com:18002"), /이 Mac/);
});

test("an unavailable prefixed URL fails instead of spawning an unreachable server", async () => {
  const value = fixture("http://127.0.0.1:18002/prefix");
  const result = await value.engine.start();
  assert.equal(result.state, "failed");
  assert.match(result.detail, /경로/);
  assert.equal(value.launches.length, 0);
});

test("an external engine is used as configured and is never stopped for the assistant", async () => {
  const value = fixture("http://localhost:80/prefix");
  value.external();
  assert.equal((await value.engine.start()).state, "external");
  assert.equal(value.requests[0].url, "http://localhost:80/prefix/health");
  await assert.rejects(value.engine.handoff.withEngineStopped(async () => {
    assert.fail("an assistant must not run with an external engine");
  }), /앱 밖에서 켠 음악 엔진/);
  await value.engine.stop();
  assert.deepEqual(value.events, []);
});

test("ACE launch passes the configured DiT and the 4B LM to the start script", async () => {
  const value = fixture();
  await value.engine.start();
  const env = value.launches[0].env!;
  assert.equal(env.MUSIC_ENGINE_ACE_PORT, "18002");
  assert.equal(env.MUSIC_ENGINE_ACE_DIT_MODEL, "acestep-v15-xl-turbo");
  assert.equal(env.MUSIC_ENGINE_ACE_LM_MODEL, "acestep-5Hz-lm-4B");
  assert.equal(env.MUSIC_ENGINE_MUSIC3_MODEL, process.env.MUSIC_ENGINE_MUSIC3_MODEL);
  await value.close();
});

test("loading ACE becomes ready only after model initialization", async () => {
  let loaded = false;
  const value = fixture("http://127.0.0.1:18002", async () => {}, {
    fetch: async () => new Response(JSON.stringify({ data: { ...ACE_OK, models_initialized: loaded, loaded_model: loaded ? ACE_OK.loaded_model : null, loaded_lm_model: null } })),
  });
  assert.equal((await value.engine.check()).state, "starting");
  assert.equal(value.engine.isReady(), false);
  assert.equal(value.engine.recentlyHealthy(), false);
  loaded = true;
  assert.equal((await value.engine.check()).state, "external");
  assert.equal(value.engine.isReady(), true);
  assert.equal(value.engine.snapshot().maxDurationSeconds, 300);
});

test("the views name the DiT and LM the server reports as loaded, and a loaded ACE can repaint", async () => {
  const value = fixture("http://127.0.0.1:18002", async () => {}, {
    fetch: async () => new Response(JSON.stringify({ data: ACE_OK })),
  });
  const status = await value.engine.check();
  assert.deepEqual(status.models, { music: "acestep-v15-xl-turbo", lm: "acestep-5Hz-lm-4B" });
  assert.equal(modelLabel(status.models.music), "ACE-Step XL turbo");
  assert.equal(modelLabel("acestep-v15-turbo"), "ACE-Step turbo");
  assert.equal(modelLabel("another-dit"), "another-dit");
  assert.match(modelLabel(null), /엔진이 켜지면/);
  assert.deepEqual(status.capabilities, { text2music: true, cover: false, repaint: true, referenceAudio: false });
  assert.equal(status.maxDurationSeconds, 300);
});

test("an owned model-loading failure is reported safely and can restart its failed HTTP process", async () => {
  const failedData = { status: "error", stage: "failed", service: "ACE-Step API",
    loaded_model: "acestep-v15-xl-turbo", loaded_lm_model: "acestep-5Hz-lm-4B",
    models_initialized: false, capabilities: { text2music: true }, error: "Bearer secret-key in an internal exception" };
  const value = fixture("http://127.0.0.1:18002", async () => {}, {
    fetch: async () => value.live()
      ? new Response(JSON.stringify({ data: value.launches.length === 1 ? failedData
        : { ...failedData, status: "ok", stage: "ready", models_initialized: true, error: null } }))
      : new Response("", { status: 503 }),
  });
  try {
    await value.engine.start();
    await drainUntil(() => value.engine.snapshot().state === "failed");
    const failed = value.engine.snapshot();
    assert.equal(failed.owned, true);
    assert.match(failed.detail, /모델을 불러오지 못했어요/);
    assert.doesNotMatch(JSON.stringify(failed), /secret-key|internal exception|Bearer/);
    assert.equal(value.engine.isReady(), false);
    assert.equal(value.engine.recentlyHealthy(), false);
    await value.engine.start();
    await drainUntil(() => value.engine.snapshot().state === "ready");
    assert.deepEqual(value.events, ["key", "spawn", "stop", "key", "spawn"]);
    assert.equal(value.launches.length, 2);
    assert.equal(value.engine.snapshot().owned, true);
  } finally { await value.close(); }
});

test("an external loading failure remains external and is never stopped, replaced, or shared with an assistant", async () => {
  const value = fixture("http://127.0.0.1:18002", async () => {}, {
    fetch: async () => new Response(JSON.stringify({ data: { status: "error", stage: "failed", service: "ACE-Step API",
      loaded_model: "acestep-v15-xl-turbo", models_initialized: false, loaded_lm_model: "acestep-5Hz-lm-4B",
      capabilities: { text2music: true }, error: "private failed-response-body" } })),
  });
  const failed = await value.engine.start();
  assert.equal(failed.state, "failed");
  assert.equal(failed.owned, false);
  assert.match(failed.detail, /앱 밖에서 켠 엔진/);
  assert.doesNotMatch(JSON.stringify(failed), /private failed-response-body/);
  await assert.rejects(value.engine.handoff.withEngineStopped(async () => assert.fail("an external failed server can still hold model memory")), /앱 밖에서 켠 음악 엔진/);
  await value.engine.stop();
  assert.deepEqual(value.events, []);
  assert.equal(value.engine.isReady(), false);
});

test("another engine, another DiT, or another LM never becomes ready", async () => {
  for (const data of [
    { engine: "minimax-music3", loaded_model: "mlx-community/MiniMax-Music3-bf16", loaded_lm_model: null },
    { service: "ACE-Step API", loaded_model: "acestep-v15-turbo", loaded_lm_model: "acestep-5Hz-lm-4B" },
    { service: "ACE-Step API", loaded_model: "acestep-v15-xl-turbo", loaded_lm_model: "acestep-5Hz-lm-1.7B" },
  ]) {
    const value = fixture("http://127.0.0.1:18002", async () => {}, {
      fetch: async () => new Response(JSON.stringify({ data: { status: "ok", models_initialized: true, ...data } })),
    });
    const status = await value.engine.start();
    assert.equal(status.state, "failed");
    assert.equal(status.owned, false);
    assert.equal(value.engine.isReady(), false);
    assert.equal(value.engine.recentlyHealthy(), false);
    await value.engine.stop();
    assert.deepEqual(value.events, [], "an external incompatible engine must never be killed or replaced");
  }
});

test("address changes are refused while an owned engine, an external engine, or startup uses memory", async () => {
  const addressA = "http://127.0.0.1:18002";
  const addressB = "http://127.0.0.1:18009";
  const value = fixture(addressA);
  await value.engine.start();
  let saved = false;
  await assert.rejects(value.engine.withConnectionChange(addressB, async () => {
    saved = true;
    value.setUrl(addressB);
  }), /꺼진 뒤 주소/);
  assert.equal(saved, false);
  assert.equal(value.engine.snapshot().baseUrl, addressA);
  assert.equal(value.live(), true);
  await value.close();

  const outside = fixture(addressA);
  outside.external();
  await assert.rejects(outside.engine.withConnectionChange(addressB, async () => { saved = true; }), /앱 밖에서 켠 엔진/);
  assert.deepEqual(outside.events, []);

  const preparing = deferred();
  const finish = deferred();
  const starting = fixture(addressA, async () => { preparing.resolve(); await finish.promise; });
  const pending = starting.engine.start();
  await preparing.promise;
  await assert.rejects(starting.engine.withConnectionChange(addressB, async () => { saved = true; }), /꺼진 뒤 주소/);
  finish.resolve();
  await pending;
  assert.equal(saved, false);
  await starting.close();
});

test("connection changes are allowed with both engines off, while unchanged-address preference writes remain usable", async () => {
  const value = fixture();
  const next = "http://127.0.0.1:18009";
  const result = await value.engine.withConnectionChange(next, async () => { value.setUrl(next); return "saved"; });
  assert.equal(result, "saved");
  await value.engine.check();
  assert.equal(value.engine.snapshot().baseUrl, next);
  assert.equal(value.engine.snapshot().state, "offline");
  await value.engine.start();
  assert.equal(await value.engine.withConnectionChange(next, async () => "same address saved"), "same address saved");
  await value.close();
});

test("an out-of-band A-to-B address change never claims an external B as app-owned or runs an assistant beside it", async () => {
  const addressA = "http://127.0.0.1:18002";
  const addressB = "http://127.0.0.1:18009";
  const value = fixture(addressA);
  await value.engine.start();
  value.external();
  value.setUrl(addressB); // Simulate preferences changed outside the guarded current IPC.
  const status = await value.engine.check();
  assert.equal(status.state, "external");
  assert.equal(status.owned, false);
  let assistantRan = false;
  await assert.rejects(value.engine.handoff.withEngineStopped(async () => { assistantRan = true; }), /앱 밖에서 켠 음악 엔진/);
  assert.equal(assistantRan, false);
  assert.equal(value.live(), true, "owned A and external B are untouched by a refused handoff");
  assert.deepEqual(value.events, ["key", "spawn"]);
  await value.close();
});

test("an external server appearing while the owned child stops prevents the assistant call", async () => {
  const value = fixture("http://127.0.0.1:18002", async () => {}, {
    kill: (child) => {
      value.external();
      value.events.push("stop");
      queueMicrotask(() => child.emit("exit", 0, "SIGTERM"));
    },
  });
  await value.engine.start();
  let assistantRan = false;
  await assert.rejects(value.engine.handoff.withEngineStopped(async () => { assistantRan = true; }), /아직 메모리를/);
  assert.equal(assistantRan, false);
  await drainUntil(() => value.engine.snapshot().state === "external");
  assert.equal(value.engine.snapshot().owned, false);
  assert.equal(value.launches.length, 1, "no external process is stopped or replaced");
});

test("a delayed poll for an old address cannot report the new address as ready", async () => {
  const pending = deferred<Response>();
  const value = fixture("http://127.0.0.1:18002", async () => {}, {
    fetch: async (input) => String(input).includes(":18002/") ? pending.promise : new Response("", { status: 503 }),
  });
  const oldPoll = value.engine.check();
  value.setUrl("http://127.0.0.1:18009");
  await value.engine.check();
  pending.resolve(new Response(JSON.stringify({ data: ACE_OK })));
  await oldPoll;
  assert.equal(value.engine.snapshot().state, "offline");
  assert.equal(value.engine.snapshot().baseUrl, "http://127.0.0.1:18009");
  assert.equal(value.engine.recentlyHealthy(), false);
});
