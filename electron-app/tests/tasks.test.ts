import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { test } from "node:test";
import { mkdtemp, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import type { TaskOutcome } from "../shared.ts";
import { completedVersions, stageDetail, stageForJob, stageText, TaskRunner, type TaskSpec } from "../main/tasks.ts";

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

test("quality completion returns only requested winners and a recommendation among new winners", async () => {
  const payload = { candidateIds: ["bestA", "bestB"], newCandidateIds: ["bestA", "bestB"],
    attemptedCandidateIds: ["rawA", "retryA", "rawB"], recommendedCandidateId: "bestB", failures: [] };
  const outcome = await run(`process.stdout.write(${JSON.stringify(JSON.stringify(payload))})`).finished;
  assert.deepEqual(outcome.newVersionIds, ["bestA", "bestB"]);
  assert.equal(outcome.recommendedVersionId, "bestB");
  assert.match(outcome.message, /버전 2개/);
  const old = await run(`process.stdout.write(${JSON.stringify(JSON.stringify({ ...payload, recommendedCandidateId: "rawA" }))})`).finished;
  assert.equal(old.recommendedVersionId, null);
});

test("quality progress counts finished requested slots rather than every successful retry", () => {
  const children = Array.from({ length: 5 }, (_, index) => ({ jobId: String(index), kind: "candidate", status: "succeeded" }));
  const job = { jobId: "batch", kind: "candidate-batch", status: "running", resultRefs: ["bestA"], parameters: { qualityPolicy: { enabled: true } } };
  assert.equal(completedVersions(job, children, 2), 1);
  assert.equal(completedVersions({ ...job, parameters: { qualityPlan: [] } }, children, 2), 1);
  assert.equal(completedVersions({ ...job, parameters: undefined, resultRefs: [], reusedCandidateIds: ["old"] }, children.slice(0, 1), 2), 2);
  assert.match(stageText("seed 12: quality_lyrics"), /가사/);
  assert.match(stageText("quality_audio"), /소리/);
  assert.match(stageText("quality_setup"), /자동 검사/);
  assert.match(stageText("quality_retry"), /다시 만드는/);
  assert.match(stageText("finalizing"), /재생본/);
  assert.match(stageForJob({ ...job, stage: "quality_retry" }, { jobId: "child", kind: "generate-candidate", status: "running", stage: "submitting" }), /다시 만드는/);
});

test("Music 3 generation phases are understandable without frame or chunk internals", () => {
  assert.equal(stageText("planning song and vocals (180/450 frames)"), "선율과 노래 흐름을 만드는 중");
  assert.equal(stageText("seed 71: rendering audio (2/4 chunks)"), "목소리와 반주를 만드는 중");
  assert.equal(stageText("saving audio"), "음원을 저장하는 중");
  assert.equal(stageDetail("planning song and vocals (180/450 frames)"), "");
  assert.equal(stageDetail("rendering audio (2/4 chunks)"), "");
  assert.equal(stageDetail("quality_lyrics: 확인 중"), "");
  assert.equal(stageDetail("seed 71: 마지막 버전 저장 중"), "마지막 버전 저장 중");
});

for (const jobKind of ["candidate-batch", "cover-batch"] as const) test(`${jobKind} refreshes a newly preferred winner even when raw candidate count does not change`, async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "music-quality-task-"));
  let resolveZero!: () => void;
  let resolveOne!: () => void;
  const zero = new Promise<void>((resolve) => { resolveZero = resolve; });
  const one = new Promise<void>((resolve) => { resolveOne = resolve; });
  let changes = 0;
  const job = { jobId: "batch", kind: jobKind, status: "running", stage: "quality_lyrics", createdAt: new Date().toISOString(),
    progress: 0.5, resultRefs: [] as string[], parameters: { qualityPolicy: { enabled: true }, qualityPlan: [{ originalSeed: 12 }] } };
  const manifest = { jobs: [job, { ...job, jobId: "child", kind: "generate-candidate", parentJobId: "batch", stage: "submitting" }], candidates: [{ candidateId: "rawA" }, { candidateId: "retryA" }] };
  await writeFile(path.join(directory, "project.json"), JSON.stringify(manifest));
  const child = spawn(process.execPath, ["-e", "process.on('SIGINT', () => process.exit(130)); setInterval(() => {}, 1000)"], { stdio: ["ignore", "pipe", "pipe"] });
  const runner = new TaskRunner({
    emit: (task) => {
      if (task?.stage.includes("가사") && task.done === 0) resolveZero();
      if (task?.done === 1) resolveOne();
    }, versionsChanged: () => { changes += 1; }, finished: () => {},
  }, () => child, 10);
  const deadline = setTimeout(() => { resolveZero(); resolveOne(); }, 2000);
  try {
    runner.start({ ...spec, kind: jobKind === "cover-batch" ? "cover" : "resume", jobKind, folder: directory });
    await zero;
    assert.equal(runner.snapshot()?.done, 0);
    job.resultRefs.push("bestA");
    await writeFile(path.join(directory, "next.json"), JSON.stringify(manifest));
    await rename(path.join(directory, "next.json"), path.join(directory, "project.json"));
    await one;
    assert.equal(runner.snapshot()?.done, 1);
    assert.equal(changes, 1);
  } finally {
    clearTimeout(deadline);
    await runner.shutdown();
    child.kill();
    await rm(directory, { recursive: true, force: true });
  }
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
