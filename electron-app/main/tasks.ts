import type { ChildProcess } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import type { ActiveTask, TaskKind, TaskOutcome } from "../shared.ts";
import { collectCli, humanizeError, spawnCli } from "./cli.ts";

export type TaskSpec = {
  kind: TaskKind;
  songId: string;
  folder: string;
  songTitle: string;
  label: string;
  args: string[];
  total: number;
  jobKind: "candidate-batch" | "cover-batch" | "repaint-candidate";
};

type RunningTask = {
  spec: TaskSpec;
  child: ChildProcess;
  startedAt: number;
  spawnedIso: string;
  candidateCount: number | null;
  cancelling: boolean;
  progress: number;
  stage: string;
  detail: string;
  done: number;
  resultShape: string;
};

type Hooks = {
  emit(task: ActiveTask | null): void;
  versionsChanged(folder: string): void;
  finished(outcome: TaskOutcome, folder: string): void;
};

type ManifestJob = {
  jobId: string;
  kind: string;
  status: string;
  stage?: string;
  progress?: number;
  parentJobId?: string | null;
  createdAt?: string;
  reusedCandidateIds?: string[];
  resultRefs?: string[];
  qualityPlan?: unknown;
  parameters?: { qualityPolicy?: { enabled?: boolean }; qualityPlan?: unknown };
};

export function stageText(raw: string | undefined): string {
  const stage = (raw ?? "").replace(/^seed -?\d+:\s*/i, "").toLowerCase();
  if (stage.includes("quality_setup")) return "자동 검사를 준비하는 중";
  if (stage.includes("quality_audio")) return "소리와 곡 길이를 확인하는 중";
  if (stage.includes("quality_lyrics")) return "가사가 빠지거나 달라졌는지 확인하는 중";
  if (stage.includes("quality_retry")) return "확실한 문제가 있어 다시 만드는 중";
  if (stage.includes("finalizing")) return "추천할 재생본을 정리하는 중";
  if (!stage || stage === "queued" || stage.includes("preparing")) return "준비하는 중";
  if (stage.includes("submitting")) return "엔진에 요청하는 중";
  if (stage.includes("reused")) return "끝난 버전을 확인하는 중";
  if (stage.includes("downloading")) return "음원을 저장하는 중";
  if (stage.includes("planning song and vocals")) return "선율과 노래 흐름을 만드는 중";
  if (stage.includes("rendering audio")) return "목소리와 반주를 만드는 중";
  if (stage.includes("saving audio")) return "음원을 저장하는 중";
  if (stage.includes("verified") || stage.includes("checking audio")) return "파일을 확인하는 중";
  if (stage.includes("cancel")) return "취소하는 중";
  return "곡을 만드는 중";
}

// Model frame/chunk counters describe calculation, not elapsed musical time.
// Keep them in the engine log and show only stages that a listener can act on.
export function stageDetail(raw: string | undefined): string {
  if (!raw || !/[가-힣]/.test(raw) || /quality_|finalizing/i.test(raw)) return "";
  return raw.replace(/^seed -?\d+:\s*/i, "").slice(0, 160);
}

export function isQualityBatch(job: ManifestJob): boolean {
  return job.parameters?.qualityPolicy?.enabled === true || Array.isArray(job.parameters?.qualityPlan) || Array.isArray(job.qualityPlan);
}

export function completedVersions(job: ManifestJob, children: ManifestJob[], total: number): number {
  const count = isQualityBatch(job) ? job.resultRefs?.length ?? 0
    : children.filter((item) => item.status === "succeeded").length + (job.reusedCandidateIds?.length ?? 0);
  return Math.min(total, Math.max(0, count));
}

export function stageForJob(job: ManifestJob, current?: ManifestJob): string {
  return stageText(/quality_|finalizing/i.test(job.stage ?? "") ? job.stage : current?.stage ?? job.stage);
}

// The app runs one generation at a time. Listening notes use independent, short
// manifest transactions and are saved immediately by the CLI.
export class TaskRunner {
  private active: RunningTask | null = null;
  private poller: NodeJS.Timeout | null = null;
  private completion: Promise<void> = Promise.resolve();

  constructor(private hooks: Hooks, private launch: typeof spawnCli = spawnCli, private pollIntervalMs = 1000) {}

  busyFolder(): string | null {
    return this.active?.spec.folder ?? null;
  }

  isBusy(folder: string): boolean {
    return this.active?.spec.folder === folder;
  }

  snapshot(): ActiveTask | null {
    const task = this.active;
    if (!task) return null;
    return {
      kind: task.spec.kind,
      songId: task.spec.songId,
      songTitle: task.spec.songTitle,
      label: task.spec.label,
      stage: task.cancelling ? "취소하는 중" : task.stage,
      detail: task.detail,
      progress: task.progress,
      done: task.done,
      total: task.spec.total,
      startedAt: task.startedAt,
      cancelling: task.cancelling,
    };
  }

  start(spec: TaskSpec): void {
    if (this.active) {
      throw new Error(`「${this.active.spec.songTitle}」을 만드는 중이에요. 끝난 뒤 다시 시도하세요.`);
    }
    const child = this.launch(spec.args);
    const task: RunningTask = {
      spec,
      child,
      startedAt: Date.now(),
      spawnedIso: new Date(Date.now() - 1000).toISOString(),
      candidateCount: null,
      cancelling: false,
      progress: 0,
      stage: "준비하는 중",
      detail: "",
      done: 0,
      resultShape: "",
    };
    this.active = task;
    this.hooks.emit(this.snapshot());
    this.poller = setInterval(() => void this.poll(task), this.pollIntervalMs);
    this.completion = collectCli(child)
      .then((result) => this.finish(task, result.code, result.stdout, result.stderr))
      .catch((error: unknown) => this.finish(task, 1, "", String(error)));
  }

  cancel(): void {
    const task = this.active;
    if (!task || task.cancelling) return;
    task.cancelling = true;
    this.hooks.emit(this.snapshot());
    // SIGINT lets the engine mark its jobs cancelled before exiting.
    task.child.kill("SIGINT");
    const escalation = setTimeout(() => {
      if (this.active === task) task.child.kill("SIGTERM");
    }, 15_000);
    escalation.unref();
  }

  // Quitting the app: cancel through the same path so project.json stays truthful.
  async shutdown(): Promise<void> {
    const task = this.active;
    if (!task) return;
    this.cancel();
    let timer: NodeJS.Timeout | undefined;
    await Promise.race([
      this.completion,
      new Promise<void>((resolve) => {
        timer = setTimeout(() => {
          if (this.active === task) task.child.kill("SIGTERM");
          resolve();
        }, 5000);
      }),
    ]);
    if (timer) clearTimeout(timer);
  }

  private async poll(task: RunningTask): Promise<void> {
    let manifest: { jobs: ManifestJob[]; candidates: unknown[] };
    try {
      manifest = JSON.parse(await readFile(path.join(task.spec.folder, "project.json"), "utf8"));
    } catch {
      return; // mid-replace or unreadable; next tick
    }
    if (this.active !== task) return;
    const job = [...manifest.jobs]
      .reverse()
      .find((item) => !item.parentJobId && item.kind === task.spec.jobKind && (item.createdAt ?? "") >= task.spawnedIso);
    if (job) {
      task.progress = Math.max(0, Math.min(1, Number(job.progress ?? 0)));
      if (["candidate-batch", "cover-batch"].includes(task.spec.jobKind)) {
        const children = manifest.jobs.filter((item) => item.parentJobId === job.jobId);
        task.done = completedVersions(job, children, task.spec.total);
        const current = [...children].reverse().find((item) => item.status === "running");
        task.stage = stageForJob(job, current);
        task.detail = stageDetail(current?.stage ?? job.stage);
      } else {
        task.stage = stageText(job.stage);
        task.detail = stageDetail(job.stage);
      }
    }
    const count = manifest.candidates.length;
    const resultShape = job && isQualityBatch(job) ? (job.resultRefs ?? []).join("|") : "";
    if ((task.candidateCount !== null && count > task.candidateCount) || (resultShape && resultShape !== task.resultShape)) this.hooks.versionsChanged(task.spec.folder);
    task.candidateCount = count;
    task.resultShape = resultShape;
    this.hooks.emit(this.snapshot());
  }

  private async finish(task: RunningTask, code: number | null, stdout: string, stderr: string): Promise<void> {
    if (this.active !== task) return;
    if (this.poller) clearInterval(this.poller);
    this.poller = null;
    let result: Record<string, unknown> = {};
    try {
      result = code === 0 ? (JSON.parse(stdout) as Record<string, unknown>) : {};
      if (!result || typeof result !== "object") throw new Error("invalid result");
      if (code === 0 && !Array.isArray(result.candidateIds) && typeof result.candidateId !== "string") throw new Error("missing candidates");
    } catch {
      code = 1;
      stderr = "엔진이 읽을 수 없는 결과를 보냈어요.";
      result = {};
    }
    const cancelled = task.cancelling || code === 130;
    const newVersionIds = Array.isArray(result.newCandidateIds)
      ? (result.newCandidateIds as string[])
      : Array.isArray(result.candidateIds)
      ? (result.candidateIds as string[])
      : typeof result.candidateId === "string"
        ? [result.candidateId]
        : [];
    const failures = Array.isArray(result.failures) ? result.failures.length : 0;
    const reused = Array.isArray(result.reusedCandidateIds) ? result.reusedCandidateIds.length : 0;
    const recommendedVersionId = typeof result.recommendedCandidateId === "string" && newVersionIds.includes(result.recommendedCandidateId)
      ? result.recommendedCandidateId : null;
    let message: string;
    if (cancelled) message = "작업을 취소했어요. 이미 끝난 버전은 남아 있어요.";
    else if (code !== 0) message = humanizeError(stderr.trim() || "작업이 실패했어요.");
    else if (task.spec.kind === "repaint") message = "고친 버전이 도착했어요. 원본과 비교해 들어 보세요.";
    else if (task.spec.kind === "cover" && !failures) message = `원본을 바탕으로 버전 ${newVersionIds.length}개를 만들었어요. 원본과 비교해 들어 보세요.`;
    else if (failures && newVersionIds.length) message = `버전 ${newVersionIds.length}개를 만들었고 ${failures}개는 실패했어요.`;
    else if (failures) message = "버전을 만들지 못했어요. 엔진 기록을 확인하세요.";
    else message = reused
      ? `기존 버전 ${reused}개를 확인하고 새 버전 ${newVersionIds.length}개를 만들었어요.`
      : `버전 ${newVersionIds.length}개를 만들었어요.`;
    if (!cancelled && code === 0 && recommendedVersionId) message += " 자동 확인 후 추천본을 골랐어요.";

    this.active = null;
    this.hooks.emit(null);
    this.hooks.finished(
      {
        kind: task.spec.kind,
        songId: task.spec.songId,
        songTitle: task.spec.songTitle,
        ok: code === 0 && !cancelled && (newVersionIds.length + reused) > 0,
        cancelled,
        message,
        newVersionIds,
        recommendedVersionId,
      },
      task.spec.folder,
    );
  }
}
