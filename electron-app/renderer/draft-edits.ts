import type { DraftResult } from "../shared.ts";
import type { CreateDraft } from "./store.ts";

type DraftKey = keyof CreateDraft;
type Ticket = { sequence: number; draft: CreateDraft; edits: Map<DraftKey, number> };

export class DraftEdits {
  private sequence = 0;
  private edits = new Map<DraftKey, number>();
  private generated = new Set<DraftKey>();

  edited(change: Partial<CreateDraft>): void {
    for (const key of Object.keys(change) as DraftKey[]) {
      this.edits.set(key, (this.edits.get(key) ?? 0) + 1);
      this.generated.delete(key);
    }
  }

  reset(): void {
    ++this.sequence;
    this.generated.clear();
    this.edits.clear();
  }

  begin(draft: CreateDraft): Ticket {
    return { sequence: ++this.sequence, draft: { ...draft }, edits: new Map(this.edits) };
  }

  isLatest(ticket: Ticket): boolean {
    return ticket.sequence === this.sequence;
  }

  merge(ticket: Ticket, current: CreateDraft, result: DraftResult, fallbackTitle: string): Partial<CreateDraft> | null {
    const unchanged = (key: DraftKey) => (this.edits.get(key) ?? 0) === (ticket.edits.get(key) ?? 0);
    if (ticket.sequence !== this.sequence || !["description", "instrumental", "durationSeconds", "productionRules", "vocalLanguage"].every((key) => unchanged(key as DraftKey))) return null;
    const values: Partial<CreateDraft> = {
      title: result.title || fallbackTitle,
      stylePrompt: result.stylePrompt,
      lyrics: current.instrumental ? "[Instrumental]" : result.lyrics,
    };
    const patch: Partial<CreateDraft> = { drafted: true };
    for (const key of Object.keys(values) as DraftKey[]) {
      if (!unchanged(key) || (String(current[key]).trim() && !this.generated.has(key))) continue;
      (patch as Record<string, unknown>)[key] = values[key];
      this.generated.add(key);
    }
    return patch;
  }
}
