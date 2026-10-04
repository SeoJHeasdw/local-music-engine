import assert from "node:assert/strict";
import type { ChildProcess, SpawnOptions } from "node:child_process";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import { aceLaunchAddress, EngineManager, type EngineDependencies } from "../main/engine.ts";

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
  baseUrl = "http://127.0.0.1:18001",
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
        ? new Response(JSON.stringify({ data: { status: "ok", models_initialized: true, llm_initialized: true } }))
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
  const engine = new EngineManager(() => baseUrl, () => {}, () => ({ dit: "dit", lm: "lm" }), beforeStart, dependencies);
  return {
    engine, events, requests, launches, dependencies,
    live: () => Boolean(live),
    external: () => { external = true; },
    close: async () => { if (live) await engine.stop(); },
  };
}

test("startup holds memory while preparing and an arriving assistant stops ACE before use", async () => {
  const preparing = deferred();
  const releasePreparation = deferred();
  let preparationCalls = 0;
  const value = fixture("http://127.0.0.1:18001", async () => {
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
  const value = fixture("http://127.0.0.1:18001", async () => { throw new Error("assistant unload failed"); });
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
  const value = fixture("http://127.0.0.1:18001", async () => {
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
  const value = fixture("http://127.0.0.1:18001", async () => {}, {
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
  const value = fixture("http://127.0.0.1:18001", async () => {
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
  const value = fixture("http://127.0.0.1:18001", async () => {}, {
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
  assert.throws(() => aceLaunchAddress("http://127.0.0.1:18001/prefix"), /경로/);
  assert.throws(() => aceLaunchAddress("https://127.0.0.1:18001"), /이 Mac/);
  assert.throws(() => aceLaunchAddress("http://example.com:18001"), /이 Mac/);
});

test("an unavailable prefixed URL fails instead of spawning an unreachable server", async () => {
  const value = fixture("http://127.0.0.1:18001/prefix");
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
