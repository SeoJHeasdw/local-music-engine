// Contract between the Electron main process and the renderer. The renderer never sees
// a file path it can act on: songs, versions and exports are addressed by ids that main
// resolves against the projects it listed itself.

export type ReviewStatus = "unreviewed" | "listened" | "approved" | "rejected";
export type Strength = "light" | "medium" | "strong";
export type TimeRange = { startSeconds: number; endSeconds: number };

export type ProductionRuleSelection = { version: 1; presetId: string | null; ruleIds: string[] };
export type ProductionRule = {
  id: string;
  label: string;
  description: string;
  caption: string;
  instrumentalCaption?: string | null;
  shortCaption?: string | null;
  shortInstrumentalCaption?: string | null;
};
export type ProductionPreset = {
  id: string;
  label: string;
  description: string;
  caption: string;
  bpm: number | null;
  keyScale: string | null;
  timeSignature: string | null;
  ruleIds: string[];
};
export type ProductionCatalog = { version: 1; presets: ProductionPreset[]; rules: ProductionRule[]; captionBudgetCharacters?: number; captionBudgetScope?: "api_prompt_character_limit" | "guidance_advisory"; captionTokenBudgetMeasured?: boolean };
export type ProductionSnapshot = {
  version: 1;
  selection: ProductionRuleSelection;
  baseStylePrompt: string;
  preset: ProductionPreset | null;
  rules: ProductionRule[];
  appliedCaptions: string[];
  appliedRuleIds: string[];
  skippedRuleIds: string[];
  inputMetas: Record<string, unknown>;
  songPlan?: SongPlanResult;
};

export type SongPlanInput = {
  lyrics: string;
  durationSeconds: number;
  bpm: number | null;
  timeSignature: string | null;
  presetId: string | null;
  development: boolean;
  breathing: boolean;
  instrumental: boolean;
};
export type SongPlanResult = {
  version: 1;
  lyricsOriginal: string;
  lyricsPrepared: string;
  changes: Array<{ kind: "line-break" | "section-guidance"; lineIndex: number; before: string; after: string }>;
  arrangement: Array<{ label: string; bars: number; startSeconds: number; endSeconds: number; energy: number; instruments: string[]; guidance: string }>;
  phrases: Array<{ section: string | null; lineIndex: number; phraseIndex: number; text: string; syllables: number; syllableMethod: "hangul-exact+english-heuristic"; estimatedBars: number; breathAfterBeats: number; targetSyllables: number }>;
  warnings: string[];
  timing: { bpm: number; timeSignature: string; quarterBeatsPerBar: number; secondsPerBar: number; estimatedTotalBars: number; bpmBeatUnit: "quarter-note" };
  options: { presetId: string | null; instrumental: boolean; development: boolean; breathing: boolean };
  guidance: string[];
};

export type RegenerateSongInput = {
  songId: string;
  versionId: string;
  stylePrompt?: string;
  lyrics?: string;
  versions: number;
};

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

export type AutomaticQuality = {
  complete?: boolean;
  status: "passed" | "attention" | "unknown";
  summary: string;
  attempt: number;
  maxAttempts: number;
  originalSeed: number;
  groupId?: string;
  preferred: boolean;
  score: number | null;
  retryReasons: string[];
  audio: Record<string, unknown>;
  lyrics: {
    status: "pass" | "warning" | "unknown" | "not_applicable";
    orderedCoverage?: number | null;
    koreanCER?: number | null;
    englishWER?: number | null;
    [key: string]: unknown;
  };
  processing: Record<string, unknown> | null;
  preparation?: { requestedDurationSeconds?: number; effectiveDurationSeconds?: number; changes?: string[] };
  observation?: { loudness?: Record<string, unknown>; separation?: Record<string, unknown>; [key: string]: unknown };
};

export type Version = {
  id: string;
  kind: "full" | "edit" | "cover";
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
  quality?: AutomaticQuality | null;
  recommended?: boolean;
  baseStylePrompt?: string;
  productionRules?: ProductionSnapshot | null;
  coverStrength?: number | null;
  songPlan?: SongPlanResult | null;
  coverSource?: { candidateId: string; artifactId: string; sha256: string; bytes: number; durationSeconds: number } | null;
  lyricsOriginal?: string;
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
  kind: "candidate-batch" | "cover-batch" | "repaint-candidate" | "export";
  status: "queued" | "running" | "succeeded" | "partial" | "failed" | "cancelling" | "cancelled" | "interrupted";
  stage: string | null;
  progress: number;
  error: string | null;
  outputCleanupErrors?: string[];
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
  productionRules?: ProductionRuleSelection | null;
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
  recommendedVersionId?: string | null;
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

export type TaskKind = "generate" | "repaint" | "resume" | "cover";

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
  recommendedVersionId?: string | null;
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
  engine: "minimax-music3";
  models: { music: string | null; precision: string | null };
  capabilities: EngineCapabilities;
  maxDurationSeconds: number;
  log: string[];
  since: number;
};

export type AssistantKind = "rules" | "ollama" | "openai";

export type EngineCapabilities = { text2music: boolean; cover: boolean; repaint: boolean; referenceAudio: boolean };

export type Settings = {
  schemaVersion: 2;
  engine: "minimax-music3";
  projectsDir: string;
  engineBaseUrl: string;
  engineAutoStart: boolean;
  musicModel: string;
  defaultVersions: number;
  defaultDurationSeconds: number;
  feedbackStrength: Strength;
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
  productionCatalog?: ProductionCatalog;
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
  productionRules?: ProductionRuleSelection;
};

export type DraftResult = {
  title: string | null;
  stylePrompt: string;
  lyrics: string;
  durationSeconds: number | null;
  bpm: number | null;
  keyScale: string | null;
  timeSignature: string | null;
  source: "llm" | "engine" | "rules";
  sourceModel: string | null;
  notes: string[];
};

export type PlanInput = {
  songId: string;
  versionId: string;
  feedback: string;
  range: TimeRange | null;
  strength: Strength;
  versions: number;
};

export type ReviewInput = {
  songId: string;
  versionId: string;
  status: ReviewStatus;
  rating?: number | null;
  note?: string;
};

export type ReviseInput = {
  songId: string;
  title?: string;
  stylePrompt?: string;
  lyrics?: string;
  durationSeconds?: number;
  bpm?: number | null;
};

export type RevealTarget =
  | { kind: "version"; versionId: string }
  | { kind: "song"; songId: string }
  | { kind: "export"; artifactId: string }
  | { kind: "songs-dir" }
  | { kind: "engine-log" };

export type ExportResult = { state: SongState; path: string };

export type MusicEvent =
  | { type: "engine"; engine: EngineStatus }
  | { type: "task"; task: ActiveTask | null }
  | { type: "task-finished"; outcome: TaskOutcome }
  | { type: "song"; songId: string; song: SongState }
  | { type: "songs"; songs: SongSummary[] };

export type MusicAppApi = {
  bootstrap(): Promise<Bootstrap>;
  listSongs(): Promise<SongSummary[]>;
  openSong(songId: string): Promise<SongState | null>;
  openSongFolder(): Promise<SongState | null>;
  closeSong(songId: string): Promise<SongState>;
  createSong(input: CreateSongInput): Promise<SongState>;
  refreshSong(songId: string): Promise<SongState>;
  review(input: ReviewInput): Promise<SongState>;
  setFinal(songId: string, versionId: string): Promise<SongState>;
  undoFinal(songId: string): Promise<SongState>;
  exportFinal(songId: string): Promise<ExportResult | null>;
  revise(input: ReviseInput): Promise<SongState>;
  draft(query: string, instrumental: boolean, durationSeconds: number, productionRules?: ProductionRuleSelection, vocalLanguage?: "ko" | "en"): Promise<DraftResult>;
  songPlan(input: SongPlanInput): Promise<SongPlanResult>;
  regenerateSong(input: RegenerateSongInput): Promise<SongState>;
  plan(input: PlanInput): Promise<Plan>;
  applyPlan(songId: string, plan: Plan, feedbackText: string): Promise<SongState>;
  generateMore(songId: string, count: number): Promise<SongState>;
  resume(songId: string, jobId: string): Promise<SongState>;
  cancelTask(songId: string, startedAt: number): Promise<void>;
  reveal(target: RevealTarget): Promise<void>;
  saveSettings(partial: Partial<Settings>): Promise<Settings>;
  pickProjectsDir(): Promise<Settings>;
  listAssistantModels(kind: AssistantKind, baseUrl: string): Promise<string[]>;
  startEngine(): Promise<EngineStatus>;
  stopEngine(): Promise<EngineStatus>;
  checkEngine(): Promise<EngineStatus>;
  onEvent(listener: (event: MusicEvent) => void): () => void;
};
