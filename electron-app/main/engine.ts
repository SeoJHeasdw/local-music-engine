import { app } from "electron";
import { spawn, type ChildProcess } from "node:child_process";
import { createWriteStream, type WriteStream } from "node:fs";
import { access, mkdir } from "node:fs/promises";
import path from "node:path";
import type { EngineStatus } from "../shared.ts";
import { aceApiBinary, aceStartScript, engineRoot } from "./paths.ts";

type Health = {
  models_initialized?: boolean;
  llm_initialized?: boolean;
  loaded_model?: string | null;
  loaded_lm_model?: string | null;
};

// The ACE server may already be running (started from a terminal). A responding server
// is used as-is and never stopped by the app; only a server this app started is owned.
export class EngineManager {
  private child: ChildProcess | null = null;
  private stopping = false;
  private log: string[] = [];
  private logStream: WriteStream | null = null;
  private monitor: NodeJS.Timeout | null = null;
  private status: EngineStatus;
  private failures = 0;
  private lastHealthyAt = 0;

  constructor(
    private baseUrl: () => string,
    private emit: (status: EngineStatus) => void,
  ) {
    this.status = this.make("checking", "엔진 상태를 확인하는 중", false);
  }

  snapshot(): EngineStatus {
    return { ...this.status, log: [...this.log.slice(-60)] };
  }

  logFile(): string {
    return path.join(app.getPath("userData"), "logs", "ace-server.log");
  }

  isReady(): boolean {
    return this.status.state === "ready" || this.status.state === "external";
  }

  // Healthy within the last minute: let the engine CLI do its own health check instead
  // of refusing a request because one poll was slow.
  recentlyHealthy(): boolean {
    return Date.now() - this.lastHealthyAt < 60_000;
  }

  private safeBaseUrl(): string {
    try {
      return this.baseUrl();
    } catch {
      return "";
    }
  }

  private make(state: EngineStatus["state"], detail: string, owned: boolean, health?: Health): EngineStatus {
    return {
      state,
      detail,
      baseUrl: this.safeBaseUrl(),
      owned,
      lmReady: Boolean(health?.llm_initialized),
      models: { dit: health?.loaded_model ?? null, lm: health?.loaded_lm_model ?? null },
      log: [],
      since: this.status && this.status.state === state ? this.status.since : Date.now(),
    };
  }

  private set(next: EngineStatus): void {
    const changed =
      next.state !== this.status.state ||
      next.detail !== this.status.detail ||
      next.models.dit !== this.status.models.dit ||
      next.lmReady !== this.status.lmReady;
    this.status = next;
    if (changed) this.emit(this.snapshot());
  }

  private async health(): Promise<Health | null> {
    try {
      // A server busy with the LM can answer slowly; a short timeout here would report it as off.
      const response = await fetch(`${this.baseUrl()}/health`, { signal: AbortSignal.timeout(6000), redirect: "error" });
      if (!response.ok) return null;
      const body = (await response.json()) as { data?: Health & { status?: string } };
      return body?.data?.status === "ok" ? body.data : null;
    } catch {
      return null;
    }
  }

  async check(): Promise<EngineStatus> {
    const health = await this.health();
    const owned = Boolean(this.child);
    if (health) {
      this.failures = 0;
      this.lastHealthyAt = Date.now();
      if (!health.models_initialized) {
        this.set(this.make("starting", "모델을 메모리에 올리는 중", owned, health));
      } else {
        this.set(this.make(owned ? "ready" : "external", owned ? "앱이 켠 엔진" : "이미 켜져 있던 엔진", owned, health));
      }
    } else if (this.child && !this.stopping) {
      this.set(this.make("starting", this.lastLogLine() || "엔진을 켜는 중", true));
    } else if (this.status.state !== "failed" && this.status.state !== "missing") {
      // A busy server can miss one poll; do not flap the whole UI on a single timeout.
      this.failures += 1;
      if (this.failures >= 3 || this.status.state === "checking") {
        this.set(this.make("offline", "켜면 모델을 메모리에 올리는 동안 잠시 걸려요.", false));
      }
    }
    return this.snapshot();
  }

  startMonitoring(): void {
    if (this.monitor) return;
    this.monitor = setInterval(() => void this.check(), 8000);
  }

  private lastLogLine(): string {
    for (let index = this.log.length - 1; index >= 0; index -= 1) {
      const line = this.log[index].trim();
      if (line) return line.length > 120 ? `${line.slice(0, 117)}…` : line;
    }
    return "";
  }

  private appendLog(chunk: string): void {
    this.logStream?.write(chunk);
    for (const line of chunk.split(/\r?\n|\r/)) {
      if (line.trim()) this.log.push(line);
    }
    if (this.log.length > 400) this.log.splice(0, this.log.length - 400);
  }

  async start(): Promise<EngineStatus> {
    const existing = await this.health();
    if (existing) return this.check();
    if (this.child) return this.snapshot();
    try {
      await access(aceApiBinary);
    } catch {
      this.set(this.make("missing", "ACE 런타임이 설치되지 않았어요. 터미널에서 ./scripts/bootstrap_ace.sh를 먼저 실행하세요.", false));
      return this.snapshot();
    }
    const port = new URL(this.baseUrl()).port || "18001";
    await mkdir(path.dirname(this.logFile()), { recursive: true });
    this.logStream = createWriteStream(this.logFile(), { flags: "a" });
    this.appendLog(`\n--- ${new Date().toISOString()} 앱에서 엔진 시작 (port ${port}) ---\n`);
    this.stopping = false;
    const child = spawn("/bin/bash", [aceStartScript], {
      cwd: engineRoot,
      env: { ...process.env, MUSIC_ENGINE_ACE_PORT: port, PYTHONUNBUFFERED: "1" },
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.child = child;
    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => this.appendLog(chunk));
    child.stderr?.on("data", (chunk: string) => this.appendLog(chunk));
    child.once("exit", (code, signal) => {
      if (this.child === child) this.child = null;
      this.logStream?.end();
      this.logStream = null;
      if (this.stopping) {
        this.set(this.make("offline", "켜면 모델을 메모리에 올리는 동안 잠시 걸려요.", false));
      } else {
        const reason = this.lastLogLine() || `종료 코드 ${signal ?? code}`;
        this.set(this.make("failed", `엔진이 멈췄어요: ${reason}`, false));
      }
      this.stopping = false;
    });
    this.set(this.make("starting", "엔진을 켜는 중", true));
    void this.waitUntilReady(child);
    return this.snapshot();
  }

  private async waitUntilReady(child: ChildProcess): Promise<void> {
    // The first start can download ~10GB of weights, so there is no short deadline here;
    // the log line shown in the UI tells the person what the server is doing.
    while (this.child === child) {
      const status = await this.check();
      if (status.state === "ready") return;
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }

  async stop(): Promise<EngineStatus> {
    const child = this.child;
    if (!child?.pid) return this.check();
    this.stopping = true;
    this.set(this.make("stopping", "엔진을 끄는 중", true));
    killGroup(child, "SIGTERM");
    const timer = setTimeout(() => killGroup(child, "SIGKILL"), 10_000);
    await new Promise<void>((resolve) => child.once("exit", () => resolve()));
    clearTimeout(timer);
    return this.snapshot();
  }

  // Called while the app quits; cannot await.
  disposeOwned(): void {
    if (this.monitor) clearInterval(this.monitor);
    if (this.child) {
      this.stopping = true;
      killGroup(this.child, "SIGTERM");
    }
  }
}

function killGroup(child: ChildProcess, signal: NodeJS.Signals): void {
  if (!child.pid) return;
  try {
    process.kill(-child.pid, signal);
  } catch {
    try {
      child.kill(signal);
    } catch {
      // already gone
    }
  }
}
