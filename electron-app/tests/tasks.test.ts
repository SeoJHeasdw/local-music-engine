import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { test } from "node:test";
import type { TaskOutcome } from "../shared.ts";
import { TaskRunner, type TaskSpec } from "../main/tasks.ts";

const spec: TaskSpec = {
  kind: "resume", songId: "test", folder: "/unused-fixture", songTitle: "테스트", label: "이어 만들기",
  args: [], total: 2, jobKind: "candidate-batch",
};

function run(script: string) {
  let finish!: (outcome: TaskOutcome) => void;
  const finished = new Promise<TaskOutcome>((resolve) => { finish = resolve; });
  const runner = new TaskRunner({ emit: () => {}, versionsChanged: () => {}, finished: (outcome) => finish(outcome) },
    () => spawn(process.execPath, ["-e", script], { stdio: ["ignore", "pipe", "pipe"] }));
  runner.start(spec);
  return { runner, finished };
}

test("resume distinguishes new versions from verified reuse", async () => {
  const payload = { candidateIds: ["old", "new"], newCandidateIds: ["new"], reusedCandidateIds: ["old"], failures: [] };
  const { runner, finished } = run(`process.stdout.write(${JSON.stringify(JSON.stringify(payload))})`);
  assert.throws(() => runner.start(spec), /만드는 중/);
  const outcome = await finished;
  assert.equal(outcome.ok, true);
  assert.deepEqual(outcome.newVersionIds, ["new"]);
  assert.match(outcome.message, /기존 버전 1개.*새 버전 1개/);
  assert.equal(runner.snapshot(), null);
});

test("a completely reused batch succeeds without pretending to create audio", async () => {
  const payload = { candidateIds: ["old"], newCandidateIds: [], reusedCandidateIds: ["old"], failures: [] };
  const { finished } = run(`process.stdout.write(${JSON.stringify(JSON.stringify(payload))})`);
  const outcome = await finished;
  assert.equal(outcome.ok, true);
  assert.deepEqual(outcome.newVersionIds, []);
  assert.match(outcome.message, /새 버전 0개/);
});

for (const output of ["not json", "null", "{}", "[]"]) {
  test(`invalid CLI output ${output} reports failure and releases the runner`, async () => {
    const { runner, finished } = run(`process.stdout.write(${JSON.stringify(output)})`);
    const outcome = await finished;
    assert.equal(outcome.ok, false);
    assert.match(outcome.message, /읽을 수 없는/);
    assert.equal(runner.snapshot(), null);
  });
}

test("quitting awaits the CLI cancellation handler", async () => {
  const child = spawn(process.execPath, ["-e", `
    process.on('SIGINT', () => setTimeout(() => process.exit(130), 80));
    process.stdout.write('ready');
    setInterval(() => {}, 1000);
  `], { stdio: ["ignore", "pipe", "pipe"] });
  const ready = once(child.stdout!, "data");
  let outcome: TaskOutcome | undefined;
  const runner = new TaskRunner({ emit: () => {}, versionsChanged: () => {}, finished: (result) => { outcome = result; } }, () => child);
  try {
    runner.start(spec);
    await ready;
    await runner.shutdown();
    assert.equal(child.exitCode, 130);
    assert.equal(outcome?.cancelled, true);
    assert.equal(runner.snapshot(), null);
  } finally {
    child.kill();
  }
});
