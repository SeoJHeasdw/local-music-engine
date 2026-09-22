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

export async function loadSong(folder: string): Promise<SongState> {
  try {
    const raw = await runCli<RawStatus>(["status", folder]);
    const versions: Version[] = await Promise.all(
      raw.candidates.map(async (candidate) => {
        versionPaths.set(candidate.candidateId, candidate.path);
        return {
          id: candidate.candidateId,
          kind: candidate.taskType === "repaint" ? "edit" : "full",
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
