import crypto from "node:crypto";
import { access, mkdir, realpath } from "node:fs/promises";
import path from "node:path";
import type {
  ExportView,
  FeedbackRecord,
  Finding,
  JobView,
  Review,
  Song,
  SongInputs,
  SongState,
  SongSummary,
  TimeRange,
  Version,
  AutomaticQuality,
  RhythmQuality,
  RhythmDiagnosticCheck,
  RhythmDiagnosticEvent,
  RhythmDiagnostics,
  ProductionSnapshot,
  SongPlanResult,
} from "../shared.ts";
import { runCli } from "./cli.ts";
import { audioUrlFor, wavPeaks } from "./audio.ts";

type RawCandidate = {
  candidateId: string;
  selected: boolean;
  seed: number | null;
  taskType: string | null;
  parentCandidateId: string | null;
  editRange: TimeRange | null;
  durationSeconds: number | null;
  artifactValid: boolean;
  artifactValidation: string;
  humanReview: Review;
  findings: Finding[];
  stylePrompt: string | null;
  lyrics: string | null;
  model: string | null;
  bpm: number | null;
  keyScale: string | null;
  repaintStrength: number | null;
  instruction: string | null;
  feedbackId: string | null;
  createdAt: string | null;
  path: string;
  quality?: Partial<AutomaticQuality> | null;
  recommended?: boolean;
  baseStylePrompt?: string | null;
  productionRules?: ProductionSnapshot | null;
  coverStrength?: number | null;
  coverSource?: Version["coverSource"];
  songPlan?: SongPlanResult | null;
  lyricsOriginal?: string | null;
};

type RawExport = ExportView & { path: string; externalPath: string | null };

type RawStatus = {
  projectId: string;
  title: string;
  path: string;
  createdAt: string | null;
  updatedAt: string | null;
  inputs: SongInputs;
  selectedCandidateId: string | null;
  recommendedCandidateId?: string | null;
  canUndoSelection: boolean;
  generationActive: boolean;
  candidates: RawCandidate[];
  feedback: FeedbackRecord[];
  jobs: JobView[];
  exports: RawExport[];
};

type RawSummary = {
  path: string;
  folderName: string;
  projectId: string | null;
  title: string;
  createdAt?: string | null;
  updatedAt?: string | null;
  stylePrompt?: string;
  durationSeconds?: number | null;
  instrumental?: boolean;
  versionCount?: number;
  editCount?: number;
  liked?: number;
  reviewed?: number;
  selectedCandidateId?: string | null;
  exported?: boolean;
  runningJobs?: number;
  error: string | null;
};

// Song ids are derived from the resolved folder path. The renderer can only name songs
// that main has listed or opened, never an arbitrary folder.
const pathBySongId = new Map<string, string>();
const versionPaths = new Map<string, string>();
const exportPaths = new Map<string, string>();

export function songIdFor(folder: string): string {
  const id = crypto.createHash("sha256").update(folder).digest("hex").slice(0, 20);
  pathBySongId.set(id, folder);
  return id;
}

export function songPath(songId: unknown): string {
  const folder = typeof songId === "string" ? pathBySongId.get(songId) : undefined;
  if (!folder) throw new Error("알 수 없는 곡이에요. 목록을 새로 고친 뒤 다시 시도하세요.");
  return folder;
}

export function versionFilePath(versionId: string): string {
  const file = versionPaths.get(versionId);
  if (!file) throw new Error("이 버전의 파일을 찾지 못했어요.");
  return file;
}

export function exportFilePath(artifactId: string): string {
  const file = exportPaths.get(artifactId);
  if (!file) throw new Error("내보낸 파일을 찾지 못했어요.");
  return file;
}

function diagnosticRecord(value: unknown): Record<string, unknown> {
  const clean = (item: unknown, depth: number): unknown => {
    if (item === null || typeof item === "boolean" || typeof item === "string") return item;
    if (typeof item === "number") return Number.isFinite(item) ? item : null;
    if (depth >= 4) return null;
    if (Array.isArray(item)) return item.slice(0, 100).map((entry) => clean(entry, depth + 1));
    if (item && typeof item === "object") return Object.fromEntries(Object.entries(item).slice(0, 100).map(([key, entry]) => [key, clean(entry, depth + 1)]));
    return null;
  };
  return value && typeof value === "object" && !Array.isArray(value) ? clean(value, 0) as Record<string, unknown> : {};
}

function diagnosticsFromStatus(value: unknown): RhythmDiagnostics | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const diagnostic = value as Record<string, unknown>;
  const versions: RhythmDiagnostics["version"][] = ["rhythm-diagnostics-v1", "rhythm-diagnostics-v2", "rhythm-diagnostics-v3", "rhythm-diagnostics-v4"];
  if (!versions.includes(diagnostic.version as RhythmDiagnostics["version"])) return null;
  const version = diagnostic.version as RhythmDiagnostics["version"];
  const measuresFullMix = version === "rhythm-diagnostics-v3" || version === "rhythm-diagnostics-v4";
  const status = (item: unknown): RhythmDiagnostics["status"] => item === "observed" || item === "needs_review" ? item : "unknown";
  const checks = diagnostic.checks && typeof diagnostic.checks === "object" && !Array.isArray(diagnostic.checks) ? diagnostic.checks as Record<string, unknown> : {};
  const check = (item: unknown, acceptedSources: string[]): RhythmDiagnosticCheck => {
    const entry = item && typeof item === "object" && !Array.isArray(item) ? item as Record<string, unknown> : {};
    const rawSource = entry.source && typeof entry.source === "object" && !Array.isArray(entry.source) ? entry.source as Record<string, unknown> : {};
    const source = typeof rawSource.kind === "string" && acceptedSources.includes(rawSource.kind)
      && typeof rawSource.method === "string" && typeof rawSource.reliability === "number" && Number.isFinite(rawSource.reliability) && rawSource.reliability >= 0 && rawSource.reliability <= 1
      ? { kind: rawSource.kind as NonNullable<RhythmDiagnosticCheck["source"]>["kind"], method: rawSource.method,
          reliability: Math.max(0, Math.min(1, rawSource.reliability)) } : null;
    const rawVocal = entry.vocalSource && typeof entry.vocalSource === "object" && !Array.isArray(entry.vocalSource) ? entry.vocalSource as Record<string, unknown> : {};
    const vocalSource: RhythmDiagnosticCheck["vocalSource"] = (rawVocal.kind === "separated_vocal" || rawVocal.kind === "synthetic_vocal") && typeof rawVocal.method === "string"
      && typeof rawVocal.reliability === "number" && Number.isFinite(rawVocal.reliability) && rawVocal.reliability >= 0 && rawVocal.reliability <= 1
      ? { kind: rawVocal.kind, method: rawVocal.method, reliability: rawVocal.reliability } : null;
    return { status: source ? status(entry.status) : "unknown", reason: typeof entry.reason === "string" ? entry.reason : "",
      source, ...(entry.vocalSource !== undefined ? { vocalSource } : {}), observed: diagnosticRecord(entry.observed) };
  };
  const durationSeconds = typeof diagnostic.durationSeconds === "number" && Number.isFinite(diagnostic.durationSeconds) && diagnostic.durationSeconds > 0 ? diagnostic.durationSeconds : null;
  const eventChecks: RhythmDiagnosticEvent["check"][] = ["beat_timing_instability_suspected", "backing_dropout_suspected", "possible_arrangement_break", "percussive_gap_observed", "mix_dropout_suspected",
    "repeated_hit_timing_shift_suspected", "smooth_timing_change_observed"];
  const categories: RhythmDiagnosticEvent["category"][] = ["beat_timing", "percussion_gap", "backing_dropout", "arrangement_break", "mix_dropout"];
  const categoryForCheck: Record<RhythmDiagnosticEvent["check"], RhythmDiagnosticEvent["category"]> = {
    beat_timing_instability_suspected: "beat_timing", backing_dropout_suspected: "backing_dropout",
    possible_arrangement_break: "arrangement_break", percussive_gap_observed: "percussion_gap",
    mix_dropout_suspected: "mix_dropout",
    repeated_hit_timing_shift_suspected: "beat_timing", smooth_timing_change_observed: "beat_timing",
  };
  const events = (items: unknown): RhythmDiagnosticEvent[] => Array.isArray(items) ? items.slice(0, 200).flatMap((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const entry = item as Record<string, unknown>;
    if (!eventChecks.includes(entry.check as RhythmDiagnosticEvent["check"]) || !categories.includes(entry.category as RhythmDiagnosticEvent["category"])
      || categoryForCheck[entry.check as RhythmDiagnosticEvent["check"]] !== entry.category
      || (entry.severity !== "warning" && entry.severity !== "info") || (entry.evidenceStatus !== "suspected" && entry.evidenceStatus !== "observed" && entry.evidenceStatus !== "intent_unknown" && entry.evidenceStatus !== "consistent_with_declared_silence")
      || typeof entry.startSeconds !== "number" || !Number.isFinite(entry.startSeconds) || entry.startSeconds < 0
      || typeof entry.endSeconds !== "number" || !Number.isFinite(entry.endSeconds) || entry.endSeconds <= entry.startSeconds
      || (durationSeconds !== null && entry.endSeconds > durationSeconds) || typeof entry.confidence !== "number" || !Number.isFinite(entry.confidence) || entry.confidence < 0 || entry.confidence > 1) return [];
    return [{ check: entry.check as RhythmDiagnosticEvent["check"], category: entry.category as RhythmDiagnosticEvent["category"], severity: entry.severity,
      evidenceStatus: entry.evidenceStatus, startSeconds: entry.startSeconds, endSeconds: entry.endSeconds,
      confidence: Math.max(0, Math.min(1, entry.confidence)), message: typeof entry.message === "string" ? entry.message : "",
      observed: diagnosticRecord(entry.observed), threshold: diagnosticRecord(entry.threshold), retryEligible: false as const }];
  }) : [];
  const beatTiming = check(checks.beatTiming, ["isolated_percussion", "percussive_estimate", "synthetic_percussion"]);
  const backingContinuity = check(checks.backingContinuity, ["separated_accompaniment", "separated_instrumental", "synthetic_accompaniment"]);
  const mixContinuity = checks.mixContinuity === undefined && !measuresFullMix ? undefined : check(checks.mixContinuity, ["full_mix_pcm"]);
  const hitTiming = checks.hitTiming === undefined && version !== "rhythm-diagnostics-v4" ? undefined : check(checks.hitTiming, ["full_mix_pcm"]);
  const eventList = events(diagnostic.events);
  const findingList = events(diagnostic.findings);
  const counts = Object.fromEntries(["totalEventCount", "omittedEventCount", "omittedWarningCount"].flatMap((key) => {
    const value = diagnostic[key];
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? [[key, value]] : [];
  }));
  const observedUnknown = status(diagnostic.status) === "observed" && (beatTiming.status === "unknown" || backingContinuity.status === "unknown"
    || mixContinuity?.status === "unknown" || hitTiming?.status === "unknown");
  const normalizedStatus = measuresFullMix && (mixContinuity?.status === "needs_review" || hitTiming?.status === "needs_review")
    ? "needs_review" : observedUnknown ? "unknown" : status(diagnostic.status);
  return { version, status: normalizedStatus,
    durationSeconds, checks: { beatTiming, backingContinuity, ...(mixContinuity ? { mixContinuity } : {}), ...(hitTiming ? { hitTiming } : {}) }, events: Array.isArray(diagnostic.events) ? eventList : findingList, findings: Array.isArray(diagnostic.findings) ? findingList : eventList,
    limitations: Array.isArray(diagnostic.limitations) ? diagnostic.limitations.filter((item): item is string => typeof item === "string") : [],
    ...(typeof diagnostic.confidenceMeaning === "string" ? { confidenceMeaning: diagnostic.confidenceMeaning } : {}),
    ...(typeof diagnostic.eventsTruncated === "boolean" ? { eventsTruncated: diagnostic.eventsTruncated } : {}), ...counts };
}

function rhythmFromStatus(value: unknown): RhythmQuality | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const rhythm = value as Record<string, unknown>;
  const positive = (number: unknown): number | null => typeof number === "number" && Number.isFinite(number) && number > 0 ? number : null;
  const confidence = (number: unknown): number => typeof number === "number" && Number.isFinite(number) ? Math.max(0, Math.min(1, number)) : 0;
  const meter = rhythm.meter && typeof rhythm.meter === "object" ? rhythm.meter as Record<string, unknown> : {};
  const frames = rhythm.frameSeries && typeof rhythm.frameSeries === "object" ? rhythm.frameSeries as Record<string, unknown> : {};
  const columns = Array.isArray(frames.columns) ? frames.columns.filter((column): column is string => typeof column === "string") : [];
  return {
    status: rhythm.status === "observed" || rhythm.status === "needs_review" ? rhythm.status : "unknown",
    ...(typeof rhythm.measuredArtifactSha256 === "string" ? { measuredArtifactSha256: rhythm.measuredArtifactSha256 } : {}),
    requestedBpm: positive(rhythm.requestedBpm),
    estimatedBpm: positive(rhythm.estimatedBpm),
    confidence: confidence(rhythm.confidence),
    ...(rhythm.diagnostics !== undefined ? { diagnostics: diagnosticsFromStatus(rhythm.diagnostics) } : {}),
    segments: Array.isArray(rhythm.segments) ? rhythm.segments.flatMap((segment) => {
      if (!segment || typeof segment !== "object") return [];
      const item = segment as Record<string, unknown>;
      if (typeof item.startSeconds !== "number" || !Number.isFinite(item.startSeconds) || item.startSeconds < 0
        || typeof item.endSeconds !== "number" || !Number.isFinite(item.endSeconds) || item.endSeconds <= item.startSeconds) return [];
      return [{ startSeconds: item.startSeconds, endSeconds: item.endSeconds, estimatedBpm: positive(item.estimatedBpm), confidence: confidence(item.confidence) }];
    }) : [],
    meter: {
      status: meter.status === "projected" && typeof meter.timeSignature === "string" && positive(meter.quarterNotesPerBar) !== null ? "projected" : "unknown",
      timeSignature: typeof meter.timeSignature === "string" ? meter.timeSignature : null,
      quarterNotesPerBar: positive(meter.quarterNotesPerBar),
      caveat: typeof meter.caveat === "string" ? meter.caveat : "실제 마디 시작과 강박은 확인되지 않았어요.",
    },
    frameSeries: {
      columns,
      points: Array.isArray(frames.points) ? frames.points.filter((point): point is Array<number | null> => Array.isArray(point)
        && point.length === columns.length && point.every((number) => number === null || (typeof number === "number" && Number.isFinite(number)))) : [],
      analysisHopSeconds: positive(frames.analysisHopSeconds) ?? 0,
      ...(positive(frames.analysisWindowSeconds) !== null ? { analysisWindowSeconds: positive(frames.analysisWindowSeconds)! } : {}),
      intervalSeconds: positive(frames.intervalSeconds) ?? 0,
    },
    findings: Array.isArray(rhythm.findings) ? rhythm.findings.flatMap((finding) => {
      if (!finding || typeof finding !== "object") return [];
      const item = finding as Record<string, unknown>;
      if (typeof item.check !== "string" || typeof item.message !== "string" || (item.severity !== "info" && item.severity !== "warning")) return [];
      return [{ check: item.check, severity: item.severity, message: item.message,
        startSeconds: typeof item.startSeconds === "number" && Number.isFinite(item.startSeconds) ? item.startSeconds : null,
        endSeconds: typeof item.endSeconds === "number" && Number.isFinite(item.endSeconds) ? item.endSeconds : null,
        value: item.value,
        ...(item.observed && typeof item.observed === "object" && !Array.isArray(item.observed) ? { observed: item.observed as Record<string, unknown> } : {}),
        ...(item.threshold && typeof item.threshold === "object" && !Array.isArray(item.threshold) ? { threshold: item.threshold as Record<string, unknown> } : {}),
        ...(typeof item.confidence === "number" ? { confidence: confidence(item.confidence) } : {}),
        retryEligible: false as const }];
    }) : [],
  };
}

export function qualityFromStatus(report: Partial<AutomaticQuality> | null | undefined): AutomaticQuality | null {
  if (!report) return null;
  return {
    ...report,
    status: report.status ?? "unknown",
    summary: report.summary ?? "자동 검사 결과가 아직 없어요.",
    attempt: report.attempt ?? 1,
    maxAttempts: report.maxAttempts ?? 4,
    originalSeed: report.originalSeed ?? 0,
    preferred: report.preferred === true,
    score: report.score ?? null,
    retryReasons: report.retryReasons ?? [],
    audio: report.audio ?? {},
    lyrics: report.lyrics ?? { status: "unknown" },
    processing: report.processing ?? null,
    rhythm: report.rhythm == null ? report.rhythm : rhythmFromStatus(report.rhythm),
  };
}

export async function validateSongFolder(input: string): Promise<string> {
  const resolved = await realpath(input);
  try {
    await access(path.join(resolved, "project.json"));
  } catch {
    throw new Error("이 폴더에는 곡(project.json)이 없어요. 곡 폴더를 고르세요.");
  }
  return resolved;
}

export async function listSongs(
  projectsDir: string,
  extras: string[],
  running: Set<string>,
): Promise<SongSummary[]> {
  const args = ["library", "--dir", projectsDir];
  for (const extra of extras) args.push("--project", extra);
  const raw = await runCli<{ songs: RawSummary[] }>(args);
  const inside = path.resolve(projectsDir);
  return raw.songs.map((item) => ({
    songId: songIdFor(item.path),
    title: item.title,
    folderName: item.folderName,
    createdAt: item.createdAt ?? null,
    updatedAt: item.updatedAt ?? null,
    stylePrompt: item.stylePrompt ?? "",
    durationSeconds: item.durationSeconds ?? null,
    instrumental: Boolean(item.instrumental),
    versionCount: item.versionCount ?? 0,
    editCount: item.editCount ?? 0,
    liked: item.liked ?? 0,
    reviewed: item.reviewed ?? 0,
    hasFinal: Boolean(item.selectedCandidateId),
    exported: Boolean(item.exported),
    running: running.has(item.path) || Boolean(item.runningJobs),
    external: path.dirname(item.path) !== inside,
    error: item.error,
  }));
}

export async function loadSong(folder: string, read: typeof runCli = runCli): Promise<SongState> {
  try {
    const raw = await read<RawStatus>(["status", folder]);
    const versions: Version[] = await Promise.all(
      raw.candidates.map(async (candidate) => {
        versionPaths.set(candidate.candidateId, candidate.path);
        return {
          id: candidate.candidateId,
          kind: candidate.coverSource || candidate.taskType === "cover" ? "cover" : candidate.taskType === "repaint" || candidate.editRange ? "edit" : "full",
          parentId: candidate.parentCandidateId,
          editRange: candidate.editRange,
          seed: candidate.seed,
          durationSeconds: candidate.durationSeconds,
          isFinal: candidate.selected,
          fileOk: candidate.artifactValid,
          fileMessage: candidate.artifactValidation,
          review: candidate.humanReview,
          findings: candidate.findings,
          stylePrompt: candidate.stylePrompt ?? raw.inputs.stylePrompt,
          lyrics: candidate.lyrics ?? raw.inputs.lyrics,
          model: candidate.model,
          bpm: candidate.bpm,
          keyScale: candidate.keyScale,
          repaintStrength: candidate.repaintStrength,
          instruction: candidate.instruction,
          feedbackId: candidate.feedbackId,
          createdAt: candidate.createdAt,
          audioUrl: candidate.artifactValid ? audioUrlFor(candidate.path) : null,
          waveform: candidate.artifactValid ? await wavPeaks(candidate.path) : [],
          quality: qualityFromStatus(candidate.quality),
          recommended: Boolean(candidate.recommended || raw.recommendedCandidateId === candidate.candidateId),
          baseStylePrompt: candidate.baseStylePrompt ?? undefined,
          productionRules: candidate.productionRules ?? null,
          coverStrength: candidate.coverStrength ?? null,
          coverSource: candidate.coverSource ?? null,
          songPlan: candidate.songPlan ?? null,
          lyricsOriginal: candidate.lyricsOriginal ?? undefined,
        } satisfies Version;
      }),
    );
    const exports = raw.exports.map((item) => {
      const target = item.externalExists && item.externalPath ? item.externalPath : item.path;
      exportPaths.set(item.artifactId, target);
      return {
        artifactId: item.artifactId,
        candidateId: item.candidateId,
        createdAt: item.createdAt,
        exists: item.exists,
        externalPath: item.externalPath,
        externalExists: item.externalExists,
      } satisfies ExportView;
    });
    const song: Song = {
      songId: songIdFor(raw.path),
      title: raw.title,
      folderName: path.basename(raw.path),
      folderPath: raw.path,
      createdAt: raw.createdAt,
      updatedAt: raw.updatedAt,
      inputs: raw.inputs,
      finalVersionId: raw.selectedCandidateId,
      recommendedVersionId: raw.recommendedCandidateId ?? null,
      canUndoFinal: raw.canUndoSelection,
      generationActive: raw.generationActive,
      versions,
      feedback: raw.feedback,
      jobs: raw.jobs,
      exports,
    };
    return { status: "ready", song };
  } catch (error) {
    return { status: "error", song: null, error: error instanceof Error ? error.message : String(error) };
  }
}

// Folder names keep the title readable in Finder while staying filesystem-safe.
export async function newSongFolder(projectsDir: string, title: string): Promise<string> {
  await mkdir(projectsDir, { recursive: true });
  const now = new Date();
  const stamp = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}`;
  const slug =
    title
      .normalize("NFC")
      .replace(/[\\/:*?"<>|\u0000-\u001f]/g, "")
      .replace(/\s+/g, "-")
      .replace(/^[.-]+|[.-]+$/g, "")
      .slice(0, 40) || "song";
  for (let attempt = 1; attempt < 100; attempt += 1) {
    const candidate = path.join(projectsDir, attempt === 1 ? `${stamp}-${slug}` : `${stamp}-${slug}-${attempt}`);
    try {
      await access(candidate);
    } catch {
      return candidate;
    }
  }
  throw new Error("새 곡 폴더 이름을 정하지 못했어요.");
}
