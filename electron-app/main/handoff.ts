// The music engine (turbo DiT + 4B LM, about 22 GB) and a local assistant LLM
// (qwen3.6:27b, about 17 GB) do not fit a 36 GB Mac together: on 2026-09-24 macOS
// terminated the engine while both were resident. The app never runs them at once.

export type Ownership = "owned" | "external" | "off";

export type HandoffEngine = {
  ownership(): Promise<Ownership>;
  stop(): Promise<unknown>;
  start(): Promise<unknown>;
};

export class MemoryHandoff {
  private tail: Promise<void> = Promise.resolve();
  private assistantCalls = 0;

  constructor(private engine: HandoffEngine) {}

  active(): boolean {
    return this.assistantCalls > 0;
  }

  // A point-in-time wait is useful for observers, but is not a lock for engine startup.
  async settled(): Promise<void> {
    let pending: Promise<void>;
    do {
      pending = this.tail;
      await pending;
    } while (pending !== this.tail);
  }

  private async exclusive<T>(use: () => Promise<T>): Promise<T> {
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise<void>((resolve) => { release = resolve; });
    await previous;
    try {
      return await use();
    } finally {
      release();
    }
  }

  // Keep the same lock from checking an existing server through unloading the assistant,
  // preparing files and spawning ACE. An assistant cannot claim memory between these steps.
  withEngineStarting<T>(start: () => Promise<T>): Promise<T> {
    return this.exclusive(start);
  }

  // Run an assistant call with the app-owned engine stopped, then bring the engine back.
  async withEngineStopped<T>(use: () => Promise<T>): Promise<T> {
    this.assistantCalls += 1;
    let restart = false;
    try {
      return await this.exclusive(async () => {
        const ownership = await this.engine.ownership();
        if (ownership === "external") {
          throw new Error(
            "앱 밖에서 켠 음악 엔진이 메모리를 쓰고 있어서 AI 도우미를 부르지 않았어요. 그 엔진을 끄거나 설정에서 규칙 도우미를 고르세요.",
          );
        }
        if (ownership === "owned") {
          restart = true;
          await this.engine.stop();
        }
        return use();
      });
    } finally {
      this.assistantCalls -= 1;
      // The shared lock is released before restart; start must acquire it again.
      if (restart) void this.engine.start().catch(() => undefined);
    }
  }
}
