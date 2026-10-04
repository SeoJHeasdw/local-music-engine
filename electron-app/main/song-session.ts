import type { SongState } from "../shared.ts";

export type SongContext = Readonly<{ folder: string; songId: string; revision: number }>;

// Opening a folder includes validation and disk reads. Only the latest request may
// become current; every operation keeps the folder it captured before its first await.
export class SongSession {
  private folder: string | null = null;
  private revision = 0;
  private opening = false;

  constructor(private idFor: (folder: string) => string) {}

  current(): SongContext | null {
    return this.folder && !this.opening
      ? { folder: this.folder, songId: this.idFor(this.folder), revision: this.revision }
      : null;
  }

  beginOpen(): number {
    this.opening = true;
    return ++this.revision;
  }

  isLatest(revision: number): boolean {
    return revision === this.revision;
  }

  cancelOpen(revision: number): void {
    if (this.isLatest(revision)) this.opening = false;
  }

  async open(
    resolveFolder: () => Promise<string | null>,
    load: (folder: string) => Promise<SongState>,
    persist: (folder: string) => Promise<unknown>,
    revision = this.beginOpen(),
  ): Promise<SongState | null> {
    const previousFolder = this.folder;
    let committed = false;
    try {
      const folder = await resolveFolder();
      if (!folder || !this.isLatest(revision)) {
        this.cancelOpen(revision);
        return null;
      }
      const state = await load(folder);
      if (!this.isLatest(revision)) return null;
      this.folder = folder;
      committed = true;
      this.idFor(folder);
      this.opening = false;
      // Persistence is enqueued in commit order, so a later open/close wins on disk too.
      await persist(folder);
      return this.isLatest(revision) ? state : null;
    } catch (error) {
      this.cancelOpen(revision);
      if (!this.isLatest(revision)) return null;
      if (committed) this.folder = previousFolder;
      throw error;
    }
  }

  close(): void {
    ++this.revision;
    this.folder = null;
    this.opening = false;
  }

  require(songId: unknown): SongContext {
    const context = this.current();
    if (!context || typeof songId !== "string" || context.songId !== songId) {
      throw new Error("열린 곡이 바뀌었어요. 현재 곡에서 다시 시도하세요.");
    }
    return context;
  }

  matches(context: SongContext): boolean {
    const current = this.current();
    return Boolean(current && current.revision === context.revision && current.folder === context.folder);
  }

  assert(context: SongContext): void {
    if (!this.matches(context)) throw new Error("열린 곡이 바뀌었어요. 현재 곡에서 다시 시도하세요.");
  }
}
