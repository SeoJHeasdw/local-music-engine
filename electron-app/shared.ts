export type Finding = {
  findingId: string;
  check: string;
  severity: "info" | "warning" | "failure";
  message: string;
  observed: Record<string, unknown>;
  threshold: Record<string, unknown>;
};

export type Review = {
  status: "unreviewed" | "listened" | "approved" | "rejected";
  rating: number | null;
  notes: Array<{ text: string; createdAt: string }>;
  updatedAt: string | null;
};

export type Candidate = {
  candidateId: string;
  selected: boolean;
  seed: number;
  taskType: "text2music" | "repaint";
  parentCandidateId: string | null;
  editRange: { startSeconds: number; endSeconds: number } | null;
  durationSeconds: number;
  artifactValid: boolean;
  artifactValidation: string;
  humanReview: Review;
  findings: Finding[];
  audioUrl: string | null;
  waveform: number[];
};

export type ProjectState = {
  status: "empty" | "ready" | "error";
  projectPath: string | null;
  projectId: string | null;
  selectedCandidateId: string | null;
  candidates: Candidate[];
  error?: string;
};

export type ReviewInput = {
  candidateId: string;
  status: Review["status"];
  rating?: number;
  note?: string;
};

export type MusicAppApi = {
  chooseProject(): Promise<ProjectState>;
  getState(): Promise<ProjectState>;
  refresh(): Promise<ProjectState>;
  review(input: ReviewInput): Promise<ProjectState>;
  select(candidateId: string): Promise<ProjectState>;
  undoSelection(): Promise<ProjectState>;
};
