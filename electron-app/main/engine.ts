import { access } from "node:fs/promises";
import path from "node:path";
import type { EngineStatus } from "../shared.ts";
import { aceAuthHeaders } from "./ace-auth.ts";
import { MemoryHandoff } from "./handoff.ts";
import { acePython, aceServer, engineRoot } from "./paths.ts";
import { ACE_LM_MODEL, ACE_MAX_DURATION } from "./settings-schema.ts";

type Health = {
  status?: "ok" | "error";
  stage?: string;
  service?: string;
  models_initialized?: boolean;
  llm_initialized?: boolean;
  loaded_model?: string | null;
  loaded_lm_model?: string | null;
};

// ACE serves text-to-music and repaint once its DiT and LM are loaded. Cover stays a
// CLI feature; the app does not offer it.
const ACE_SERVICE = "ACE-Step API";
const OFF_DETAIL = "곡을 만들 때 켜고, 다 만들면 꺼서 메모리를 돌려줘요.";

// The generation run writes the log of the server it starts (src/local_music_engine/ace_runtime.py).
export const aceLogFile = path.join(engineRoot, ".runtime", "logs", "ace-server.log");

export type EngineDependencies = {
  fetch: typeof fetch;
  installed(): Promise<unknown>;
  authHeaders(): Promise<Record<string, string>>;
  pause(milliseconds: number): Promise<void>;
};

const defaultDependencies: EngineDependencies = {
  fetch: (...args) => fetch(...args),
  installed: async () => { await access(acePython); await access(aceServer); },
  authHeaders: aceAuthHeaders,
  pause: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
};

// ACE runs only while a song is being made: the run (the CLI this app starts) brings it up
// and the server stops itself when the run ends. The app watches it and never owns it.
export class EngineManager {
  private monitor: NodeJS.Timeout | null = null;
  private status: EngineStatus;
  private failures = 0;
  private dependencies: EngineDependencies;
  // Assistant LLM calls run only while no music engine holds memory; see handoff.ts.
  readonly handoff = new MemoryHandoff({
    running: async () => !["offline", "missing"].includes((await this.check()).state),
    pause: (milliseconds) => this.dependencies.pause(milliseconds),
  });

  constructor(
    private baseUrl: () => string,
    private emit: (status: EngineStatus) => void,
    private model: () => string,
    dependencies: Partial<EngineDependencies> = {},
  ) {
    this.dependencies = { ...defaultDependencies, ...dependencies };
    this.status = this.make("checking", "엔진 상태를 확인하는 중");
  }

  snapshot(): EngineStatus {
    return { ...this.status };
  }

  logFile(): string {
    return aceLogFile;
  }

  private safeBaseUrl(): string {
    try {
      return this.baseUrl();
    } catch {
      return "";
    }
  }

  private make(state: EngineStatus["state"], detail: string, health?: Health): EngineStatus {
    return {
      state,
      detail,
      baseUrl: this.safeBaseUrl(),
      engine: "ace-step",
      models: { music: health?.loaded_model ?? null, lm: health?.loaded_lm_model ?? null },
      capabilities: { text2music: health?.models_initialized === true, cover: false, repaint: health?.models_initialized === true, referenceAudio: false },
      maxDurationSeconds: ACE_MAX_DURATION,
      since: this.status && this.status.state === state ? this.status.since : Date.now(),
    };
  }

  private set(next: EngineStatus): void {
    const changed =
      next.state !== this.status.state ||
      next.detail !== this.status.detail ||
      next.baseUrl !== this.status.baseUrl ||
      next.models.music !== this.status.models.music ||
      next.models.lm !== this.status.models.lm ||
      next.capabilities.text2music !== this.status.capabilities.text2music;
    this.status = next;
    if (changed) this.emit(this.snapshot());
  }

  private async health(baseUrl = this.baseUrl()): Promise<Health | null> {
    try {
      // A server busy with the LM can answer slowly; a short timeout here would report it as off.
      const response = await this.dependencies.fetch(`${baseUrl}/health`, {
        headers: await this.dependencies.authHeaders(),
        signal: AbortSignal.timeout(6000),
        redirect: "error",
      });
      if (!response.ok) return null;
      const body = (await response.json()) as { data?: Health };
      // A model-loading failure leaves the HTTP process alive. Preserve that
      // state without forwarding its exception text or response body to UI.
      return body?.data?.status === "ok" || body?.data?.status === "error" ? body.data : null;
    } catch {
      return null;
    }
  }

  async check(): Promise<EngineStatus> {
    const checkedUrl = this.baseUrl();
    const health = await this.health(checkedUrl);
    // Ignore a poll for an address that changed while its response was in flight.
    if (checkedUrl !== this.baseUrl()) return this.snapshot();
    if (health) {
      this.failures = 0;
      const mismatch = this.modelMismatch(health);
      if (mismatch) this.set(this.make("failed", mismatch, health));
      else if (health.status === "error" || health.stage === "failed") {
        this.set(this.make("failed", "음악 모델을 불러오지 못했어요. 엔진 기록을 확인하세요. 터미널에서 켠 엔진이면 그곳에서 끈 뒤 다시 만들어 보세요.", health));
      } else if (!health.models_initialized) this.set(this.make("starting", "모델을 메모리에 올리는 중", health));
      else this.set(this.make("ready", "곡을 만드는 동안 켜져 있어요. 다 만들면 저절로 꺼져요.", health));
      return this.snapshot();
    }
    try {
      await this.dependencies.installed();
    } catch {
      this.set(this.make("missing", "ACE-Step이 아직 설치되지 않았어요. scripts/bootstrap_ace.sh로 설치해 주세요."));
      return this.snapshot();
    }
    // A busy server can miss one poll; do not flap the whole UI on a single timeout.
    this.failures += 1;
    if (this.failures >= 3 || !["ready", "starting"].includes(this.status.state)) this.set(this.make("offline", OFF_DETAIL));
    return this.snapshot();
  }

  startMonitoring(): void {
    if (this.monitor) return;
    this.monitor = setInterval(() => void this.check(), 8000);
  }

  dispose(): void {
    if (this.monitor) clearInterval(this.monitor);
    this.monitor = null;
  }

  private modelMismatch(health: Health): string | null {
    if (health.service !== ACE_SERVICE) return "다른 음악 엔진이 이 주소에 켜져 있어요. ACE-Step 주소를 확인하세요.";
    const loaded = [health.loaded_model, health.loaded_lm_model];
    if ((!loaded[0] || loaded[0] === this.model()) && (!loaded[1] || loaded[1] === ACE_LM_MODEL)) return null;
    return `설정과 다른 모델이 켜져 있어요 (${loaded.filter(Boolean).join(" · ")}). 그 엔진이 꺼진 뒤 만들면 설정한 모델로 켜요.`;
  }
}
