import type { TagChange } from "../shared.ts";
import { splitTags } from "./format.ts";

// Instruments a non-musician can name, as the style tag the music model reads. `match`
// finds the instrument inside an existing prompt ("warm piano" counts as piano, but
// "electric piano" does not), so removing it drops every tag that mentions it.
export type Instrument = { label: string; tag: string; match: RegExp };

export const INSTRUMENTS: Instrument[] = [
  { label: "피아노", tag: "piano", match: /(?<!electric )(?<!e-)\bpiano\b/i },
  { label: "일렉 피아노", tag: "electric piano", match: /\b(electric piano|e-piano|rhodes)\b/i },
  { label: "어쿠스틱 기타", tag: "acoustic guitar", match: /\bacoustic guitars?\b/i },
  { label: "일렉 기타", tag: "electric guitar", match: /\b(electric|clean|distorted|funky) guitars?\b/i },
  { label: "베이스", tag: "groovy bassline", match: /(?<!808 )\bbass(line|lines| guitar)?\b/i },
  { label: "808", tag: "808 bass", match: /\b808s?\b/i },
  { label: "신스", tag: "synth pads", match: /\bsynth(s|esizers?)?\b/i },
  { label: "현악기", tag: "strings", match: /\b(strings|string section|violins?|cellos?)\b/i },
  { label: "브라스", tag: "brass section", match: /\b(brass|horn section|trumpets?)\b/i },
  { label: "색소폰", tag: "saxophone", match: /\b(sax|saxophone)\b/i },
];

export function hasInstrument(style: string, instrument: Instrument): boolean {
  return instrument.match.test(style);
}

export function addInstrument(style: string, instrument: Instrument): string {
  return hasInstrument(style, instrument) ? style : [...splitTags(style), instrument.tag].join(", ");
}

export function removeInstrument(style: string, instrument: Instrument): string {
  return splitTags(style).filter((tag) => !instrument.match.test(tag)).join(", ");
}

// Applies pending add/remove choices to a prompt and reports them as tag changes, the
// same shape the fix assistant uses, so the plan card can show and undo each one.
export function applyInstrumentChanges(style: string, changes: Map<string, "add" | "remove">): { style: string; changes: TagChange[] } {
  let next = style;
  const reported: TagChange[] = [];
  for (const instrument of INSTRUMENTS) {
    const op = changes.get(instrument.tag);
    if (op === "add" && !hasInstrument(next, instrument)) {
      next = addInstrument(next, instrument);
      reported.push({ op: "add", term: instrument.tag, label: instrument.label });
    } else if (op === "remove") {
      for (const tag of splitTags(next).filter((item) => instrument.match.test(item))) reported.push({ op: "remove", term: tag, label: instrument.label });
      next = removeInstrument(next, instrument);
    }
  }
  return { style: next, changes: reported };
}
