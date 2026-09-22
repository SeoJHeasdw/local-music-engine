import type {
  ActiveTask,
  AppInfo,
  EngineStatus,
  Plan,
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
};

export type State = {
  view: View;
  settings: Settings;
  engine: EngineStatus;
  songs: SongSummary[];
  song: SongState;
  task: ActiveTask | null;
  rules: RuleHint[];
  info: AppInfo;
  activeVersionId: string | null;
  selection: TimeRange | null;
  listenToParent: boolean;
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

export function initStore(initial: State): void {
  state = initial;
}

export function get(): State {
  return state;
}

// Shallow merge + notify with the set of changed keys so views re-render only what moved.
export function set(patch: Partial<State>): void {
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
