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
  private current: Promise<void> | null = null;

  constructor(private engine: HandoffEngine) {}

  active(): boolean {
    return this.current !== null;
  }

  // Resolves once no assistant call holds the memory; the engine waits here before starting.
  async settled(): Promise<void> {
    while (this.current) await this.current;
  }

  // Run an assistant call with the app-owned engine stopped, then bring the engine back.
  async withEngineStopped<T>(use: () => Promise<T>): Promise<T> {
    // No await between this check and claiming `current`, so concurrent callers queue.
    while (this.current) await this.current;
    let release!: () => void;
    this.current = new Promise<void>((resolve) => {
      release = resolve;
    });
    let restart = false;
    try {
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
      return await use();
    } finally {
      this.current = null;
      release();
      if (restart) void this.engine.start();
    }
  }
}
