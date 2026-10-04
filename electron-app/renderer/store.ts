import type {
  ActiveTask,
  AppInfo,
  EngineStatus,
  Plan,
  ProductionCatalog,
  ProductionRuleSelection,
  RuleHint,
  Settings,
  SongState,
  SongSummary,
  Strength,
  TimeRange,
} from "../shared.ts";

export type View = "library" | "create" | "studio" | "settings";
export type InspectorTab = "fix" | "review" | "details";

export type CreateDraft = {
  description: string;
  instrumental: boolean;
  title: string;
  stylePrompt: string;
  lyrics: string;
  durationSeconds: number;
  versions: number;
  bpm: string;
  keyScale: string;
  timeSignature: string;
  drafted: boolean;
  productionRules?: ProductionRuleSelection;
  vocalLanguage?: "ko" | "en";
};

export type State = {
  view: View;
  settings: Settings;
  engine: EngineStatus;
  songs: SongSummary[];
  song: SongState;
  task: ActiveTask | null;
  rules: RuleHint[];
  productionCatalog?: ProductionCatalog;
  info: AppInfo;
  activeVersionId: string | null;
  selection: TimeRange | null;
  listenToParent: boolean;
  comparisonVersionId: string | null;
  loop: boolean;
  tab: InspectorTab;
  feedback: string;
  scope: "song" | "range";
  strength: Strength;
  planVersions: number;
  plan: Plan | null;
  planning: boolean;
  create: CreateDraft;
};

type Listener = (state: State, changed: Set<keyof State>) => void;

let state: State;
const listeners = new Set<Listener>();
let songRevision = 0;
let openingSong = false;
let planRevision = 0;
let listeningVersionRevision = 0;

export function listeningRevision(): number {
  return listeningVersionRevision;
}

export type SongTicket = Readonly<{ songId: string; revision: number }>;
export type PlanTicket = Readonly<{ song: SongTicket; revision: number }>;

export function beginSongOpen(): number {
  openingSong = true;
  ++planRevision;
  ++listeningVersionRevision;
  const revision = ++songRevision;
  set({ plan: null });
  return revision;
}

export function finishSongOpen(revision: number): boolean {
  if (revision !== songRevision) return false;
  openingSong = false;
  return true;
}

export function songTicket(songId = state.song.song?.songId): SongTicket | null {
  if (openingSong || !songId || state.song.song?.songId !== songId) return null;
  return { songId, revision: songRevision };
}

export function isSongTicket(ticket: SongTicket | null): ticket is SongTicket {
  return Boolean(ticket && !openingSong && ticket.revision === songRevision && state.song.song?.songId === ticket.songId);
}

export function planTicket(): PlanTicket | null {
  const song = songTicket();
  return song ? { song, revision: planRevision } : null;
}

export function isPlanTicket(ticket: PlanTicket | null): boolean {
  return Boolean(ticket && isSongTicket(ticket.song) && ticket.revision === planRevision);
}

export function initStore(initial: State): void {
  state = initial;
  ++songRevision;
  ++planRevision;
  ++listeningVersionRevision;
  openingSong = false;
}

export function get(): State {
  return state;
}

// Shallow merge + notify with the set of changed keys so views re-render only what moved.
export function set(patch: Partial<State>, passive = false): void {
  if (!passive && (("activeVersionId" in patch && patch.activeVersionId !== state.activeVersionId)
    || ("listenToParent" in patch && patch.listenToParent !== state.listenToParent)
    || ("comparisonVersionId" in patch && patch.comparisonVersionId !== state.comparisonVersionId))) ++listeningVersionRevision;
  const songChanged = patch.song !== undefined && patch.song.song?.songId !== state.song.song?.songId;
  if (songChanged) ++songRevision;
  const planKeys: Array<keyof State> = ["activeVersionId", "selection", "scope", "strength", "planVersions", "feedback"];
  const planChanged = songChanged || planKeys.some((key) => key in patch && patch[key] !== state[key])
    || (patch.settings !== undefined && JSON.stringify(patch.settings.assistant) !== JSON.stringify(state.settings.assistant));
  if (planChanged) {
    ++planRevision;
    patch = { ...patch, plan: null };
  }
  const changed = new Set<keyof State>();
  for (const key of Object.keys(patch) as Array<keyof State>) {
    if (state[key] !== patch[key]) changed.add(key);
  }
  if (!changed.size) return;
  state = { ...state, ...patch };
  for (const listener of listeners) listener(state, changed);
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
