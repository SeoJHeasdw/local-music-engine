// Contract between the Electron main process and the renderer. The renderer never sees
// a file path it can act on: songs, versions and exports are addressed by ids that main
// resolves against the projects it listed itself.

export type ReviewStatus = "unreviewed" | "listened" | "approved" | "rejected";
export type Strength = "light" | "medium" | "strong";
export type TimeRange = { startSeconds: number; endSeconds: number };

export type Finding = {
  findingId: string;
  check: string;
  severity: "info" | "warning" | "failure";
  message: string;
  observed: Record<string, unknown>;
  threshold: Record<string, unknown>;
  startSeconds: number | null;
  endSeconds: number | null;
};

export type Review = {
  status: ReviewStatus;
  rating: number | null;
  notes: Array<{ text: string; createdAt: string }>;
  updatedAt: string | null;
};

export type Version = {
  id: string;
  kind: "full" | "edit";
  parentId: string | null;
  editRange: TimeRange | null;
  seed: number | null;
  durationSeconds: number | null;
  isFinal: boolean;
  fileOk: boolean;
  fileMessage: string;
  review: Review;
  findings: Finding[];
  stylePrompt: string;
  lyrics: string;
  model: string | null;
  bpm: number | null;
  keyScale: string | null;
  repaintStrength: number | null;
  instruction: string | null;
  feedbackId: string | null;
  createdAt: string | null;
  audioUrl: string | null;
  waveform: number[];
};

export type TagChange = { op: "add" | "remove"; term: string; label?: string };

export type Plan = {
  action: "repaint" | "regenerate";
  summary: string;
  stylePrompt: string;
  baseStylePrompt: string;
  lyrics: string | null;
  bpm: number | null;
  range: TimeRange | null;
  strength: Strength;
  versions: number;
  changes: TagChange[];
  assistant: "rules" | "llm";
  assistantModel: string | null;
  understood: boolean;
  notes: string[];
  candidateId: string;
};

export type FeedbackRecord = {
  feedbackId: string;
  candidateId: string | null;
  text: string;
  range: TimeRange | null;
  plan: Plan | null;
  jobId: string;
  createdAt: string;
};

export type JobView = {
  jobId: string;
  kind: "candidate-batch" | "repaint-candidate" | "export";
  status: "queued" | "running" | "succeeded" | "partial" | "failed" | "cancelling" | "cancelled" | "interrupted";
  stage: string | null;
  progress: number;
  error: string | null;
  createdAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  feedbackId: string | null;
  resultRefs: string[];
  seeds?: number[];
  succeeded?: number;
  failed?: number;
  reused?: number;
  failures?: Array<{ seed: string; error: string }>;
  parentCandidateId?: string;
  candidateId?: string;
  resumeOfJobId?: string | null;
  resumedByJobId?: string | null;
  canResume?: boolean;
  resumeBlockedReason?: string | null;
};

export type ExportView = {
  artifactId: string;
  candidateId: string | null;
  createdAt: string | null;
  exists: boolean;
  externalPath: string | null;
  externalExists: boolean;
};

export type SongInputs = {
  lyrics: string;
  stylePrompt: string;
  durationSeconds: number;
  structure: string | null;
  bpm: number | null;
  keyScale: string | null;
  timeSignature: string | null;
};

export type Song = {
  songId: string;
  title: string;
  folderName: string;
  folderPath: string;
  createdAt: string | null;
  updatedAt: string | null;
  inputs: SongInputs;
  finalVersionId: string | null;
  canUndoFinal: boolean;
  generationActive: boolean;
  versions: Version[];
  feedback: FeedbackRecord[];
  jobs: JobView[];
  exports: ExportView[];
};

export type SongState = {
  status: "none" | "ready" | "error";
  song: Song | null;
  error?: string;
};

export type SongSummary = {
  songId: string;
  title: string;
  folderName: string;
  createdAt: string | null;
  updatedAt: string | null;
  stylePrompt: string;
  durationSeconds: number | null;
  instrumental: boolean;
  versionCount: number;
  editCount: number;
  liked: number;
  reviewed: number;
  hasFinal: boolean;
  exported: boolean;
  running: boolean;
  external: boolean;
  error: string | null;
};

export type TaskKind = "generate" | "repaint" | "resume";

export type ActiveTask = {
  kind: TaskKind;
  songId: string;
  songTitle: string;
  label: string;
  stage: string;
  detail: string;
  progress: number;
  done: number;
  total: number;
  startedAt: number;
  cancelling: boolean;
};

export type TaskOutcome = {
  kind: TaskKind;
  songId: string;
  songTitle: string;
  ok: boolean;
  cancelled: boolean;
  message: string;
  newVersionIds: string[];
};

export type EngineState =
  | "checking"
  | "offline"
  | "starting"
  | "ready"
  | "external"
  | "stopping"
  | "failed"
  | "missing";

export type EngineStatus = {
  state: EngineState;
  detail: string;
  baseUrl: string;
  owned: boolean;
  lmReady: boolean;
  models: { dit: string | null; lm: string | null };
  log: string[];
  since: number;
};

export type AssistantKind = "rules" | "ollama" | "openai";

export type Settings = {
  projectsDir: string;
  aceBaseUrl: string;
  aceAutoStart: boolean;
  ditModel: string;
  lmModel: string;
  defaultVersions: number;
  defaultDurationSeconds: number;
  repaintStrength: Strength;
  assistant: { kind: AssistantKind; baseUrl: string; model: string };
  lastExportDir: string | null;
};

export type RuleHint = { key: string; label: string; example: string; local: boolean };

export type AppInfo = {
  version: string;
  engineRoot: string;
  dataDir: string;
};

export type Bootstrap = {
  settings: Settings;
  engine: EngineStatus;
  songs: SongSummary[];
  song: SongState;
  task: ActiveTask | null;
  rules: RuleHint[];
  info: AppInfo;
};

export type CreateSongInput = {
  title: string;
  stylePrompt: string;
  lyrics: string;
  durationSeconds: number;
  versions: number;
  bpm: number | null;
  keyScale: string | null;
  timeSignature: string | null;
};

export type DraftResult = {
  title: string | null;
  stylePrompt: string;
  lyrics: string;
  durationSeconds: number | null;
  bpm: number | null;
  keyScale: string | null;
  timeSignature: string | null;
  source: "llm" | "engine";
  sourceModel: string | null;
  notes: string[];
};

export type PlanInput = {
  versionId: string;
  feedback: string;
  range: TimeRange | null;
  strength: Strength;
  versions: number;
};

export type ReviewInput = {
  versionId: string;
  status: ReviewStatus;
  rating?: number | null;
  note?: string;
};

export type ReviseInput = {
  title?: string;
  stylePrompt?: string;
  lyrics?: string;
  durationSeconds?: number;
  bpm?: number | null;
};

export type RevealTarget =
  | { kind: "version"; versionId: string }
  | { kind: "song"; songId?: string }
  | { kind: "export"; artifactId: string }
  | { kind: "songs-dir" }
  | { kind: "engine-log" };

export type ExportResult = { state: SongState; path: string };

export type MusicEvent =
  | { type: "engine"; engine: EngineStatus }
  | { type: "task"; task: ActiveTask | null }
  | { type: "task-finished"; outcome: TaskOutcome }
  | { type: "song"; song: SongState }
  | { type: "songs"; songs: SongSummary[] };

export type MusicAppApi = {
  bootstrap(): Promise<Bootstrap>;
  listSongs(): Promise<SongSummary[]>;
  openSong(songId: string): Promise<SongState>;
  openSongFolder(): Promise<SongState | null>;
  closeSong(): Promise<SongState>;
  createSong(input: CreateSongInput): Promise<SongState>;
  refreshSong(): Promise<SongState>;
  review(input: ReviewInput): Promise<SongState>;
  setFinal(versionId: string): Promise<SongState>;
  undoFinal(): Promise<SongState>;
  exportFinal(): Promise<ExportResult | null>;
  revise(input: ReviseInput): Promise<SongState>;
  draft(query: string, instrumental: boolean, durationSeconds: number): Promise<DraftResult>;
  plan(input: PlanInput): Promise<Plan>;
  applyPlan(plan: Plan, feedbackText: string): Promise<SongState>;
  generateMore(count: number): Promise<SongState>;
  resume(jobId: string): Promise<SongState>;
  cancelTask(): Promise<void>;
  reveal(target: RevealTarget): Promise<void>;
  saveSettings(partial: Partial<Settings>): Promise<Settings>;
  pickProjectsDir(): Promise<Settings>;
  listAssistantModels(kind: AssistantKind, baseUrl: string): Promise<string[]>;
  startEngine(): Promise<EngineStatus>;
  stopEngine(): Promise<EngineStatus>;
  checkEngine(): Promise<EngineStatus>;
  onEvent(listener: (event: MusicEvent) => void): () => void;
};
