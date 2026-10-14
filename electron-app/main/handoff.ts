// Music generation and a local assistant can exceed the available memory together.
// ACE runs only while a song is being made, so an assistant call waits for it to be gone
// and a generation starts only after the assistant's model is out of memory.

export type HandoffEngine = {
  running(): Promise<boolean>;
  pause(milliseconds: number): Promise<void>;
};

// A server whose run just ended stops itself within a few seconds.
const ENGINE_EXIT_WAIT_MS = 10_000;
const ENGINE_EXIT_POLL_MS = 1000;

export class MemoryHandoff {
  private tail: Promise<void> = Promise.resolve();
  private assistantCalls = 0;

  constructor(private engine: HandoffEngine) {}

  active(): boolean {
    return this.assistantCalls > 0;
  }

  // A point-in-time wait is useful for observers, but is not a lock for a generation start.
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

  // Unloading the assistant and starting the run that loads ACE share one lock, so an
  // assistant cannot claim memory between these steps.
  withEngineStarting<T>(start: () => Promise<T>): Promise<T> {
    return this.exclusive(start);
  }

  // Run an assistant call only while no music engine holds memory.
  async withEngineOff<T>(use: () => Promise<T>): Promise<T> {
    this.assistantCalls += 1;
    try {
      return await this.exclusive(async () => {
        for (let waited = 0; await this.engine.running(); waited += ENGINE_EXIT_POLL_MS) {
          if (waited >= ENGINE_EXIT_WAIT_MS) {
            throw new Error(
              "음악 엔진이 아직 메모리를 쓰고 있어서 AI 도우미를 부르지 않았어요. 터미널에서 곡을 만드는 중이면 끝난 뒤, 터미널에서 직접 켠 엔진이면 그 엔진을 끈 뒤 다시 시도하세요.",
            );
          }
          await this.engine.pause(ENGINE_EXIT_POLL_MS);
        }
        return use();
      });
    } finally {
      this.assistantCalls -= 1;
    }
  }
}
