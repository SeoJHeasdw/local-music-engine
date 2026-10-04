import assert from "node:assert/strict";
import { test } from "node:test";
import { unloadAssistant } from "../main/assistant-model.ts";

const assistant = { kind: "ollama" as const, baseUrl: "http://127.0.0.1:11434", model: "local" };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const loaded = () => json({ models: [{ name: "local:latest" }] });

function responses(items: Response[]): typeof fetch {
  return (async () => {
    const next = items.shift();
    assert.ok(next, "unexpected model request");
    return next;
  }) as typeof fetch;
}

test("model unload requires acknowledgement and verifies the resident list", async () => {
  const requests: string[] = [];
  const reply = responses([loaded(), json({ done: true }), json({ models: [] })]);
  await unloadAssistant(assistant, (async (input, options) => {
    requests.push(String(input));
    if (options?.method === "POST") assert.deepEqual(JSON.parse(String(options.body)), { model: "local", keep_alive: 0 });
    return reply(input, options);
  }) as typeof fetch);
  assert.deepEqual(requests.map((url) => new URL(url).pathname), ["/api/ps", "/api/generate", "/api/ps"]);
});

test("HTTP errors and incomplete unloads fail before music engine startup", async () => {
  await assert.rejects(unloadAssistant(assistant, responses([json({}, 500)])), /HTTP 500/);
  await assert.rejects(unloadAssistant(assistant, responses([loaded(), json({}, 503)])), /HTTP 503/);
  await assert.rejects(unloadAssistant(assistant, responses([loaded(), json({ done: false })])), /아직 메모리에/);
  await assert.rejects(unloadAssistant(assistant, responses([loaded(), json({ done: true }), loaded()])), /아직 메모리에/);
  await assert.rejects(unloadAssistant(assistant, responses([json({})])), /응답을 읽지/);
});
