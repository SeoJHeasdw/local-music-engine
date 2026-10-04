import type { SongPlanInput, SongPlanResult } from "../shared.ts";

type PreviewTicket = { sequence: number; revision: number; input: SongPlanInput };

// Even an edit followed by restoring the previous text invalidates an outstanding read.
export class SongPlanPreview {
  private revision = 0;
  private sequence = 0;
  private cached: { ticket: PreviewTicket; result: SongPlanResult } | null = null;

  edited(): void { ++this.revision; this.cached = null; }
  reset(): void { this.edited(); ++this.sequence; }
  begin(input: SongPlanInput): PreviewTicket { return { sequence: ++this.sequence, revision: this.revision, input: { ...input } }; }
  isCurrent(ticket: PreviewTicket): boolean { return ticket.sequence === this.sequence && ticket.revision === this.revision; }
  complete(ticket: PreviewTicket, result: SongPlanResult): boolean {
    if (!this.isCurrent(ticket)) return false;
    this.cached = { ticket, result };
    return true;
  }
  current(input: SongPlanInput): SongPlanResult | null {
    return this.cached && this.isCurrent(this.cached.ticket) && JSON.stringify(this.cached.ticket.input) === JSON.stringify(input) ? this.cached.result : null;
  }
}
