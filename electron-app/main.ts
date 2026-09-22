import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  protocol,
} from "electron";
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import { access, mkdir, readFile, realpath, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { Candidate, ProjectState, ReviewInput } from "./shared.ts";

protocol.registerSchemesAsPrivileged([
  {
    scheme: "music-artifact",
    privileges: { secure: true, standard: true, stream: true, supportFetchAPI: true },
  },
]);

const distRoot = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(distRoot, "../..");
const cliPath = path.join(projectRoot, ".venv", "bin", "music-engine");
const artifactTokens = new Map<string, string>();
const candidateIdPattern = /^candidate_[a-f0-9]{32}$/;
let activeProjectPath: string | null = null;
let mainWindow: BrowserWindow | null = null;

function settingsPath(): string {
  return path.join(app.getPath("userData"), "state.json");
}

async function saveLastProject(projectPath: string): Promise<void> {
  const destination = settingsPath();
  await mkdir(path.dirname(destination), { recursive: true });
  const temporary = `${destination}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify({ projectPath }, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await import("node:fs/promises").then(({ rename }) => rename(temporary, destination));
}

async function readLastProject(): Promise<string | null> {
  try {
    const raw = JSON.parse(await readFile(settingsPath(), "utf8")) as unknown;
    return typeof raw === "object" && raw !== null && "projectPath" in raw
      ? String((raw as { projectPath: unknown }).projectPath)
      : null;
  } catch {
    return null;
  }
}

async function validateProjectDirectory(input: string): Promise<string> {
  const resolved = await realpath(input);
  await access(path.join(resolved, "project.json"));
  return resolved;
}

function runCli(args: string[]): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const child = spawn(cliPath, args, {
      cwd: projectRoot,
      env: { ...process.env, PYTHONUTF8: "1" },
      stdio: ["ignore", "pipe", "pipe"],
      shell: false,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.once("error", reject);
    child.once("close", (code) => {
      if (code !== 0) {
        reject(new Error(stderr.trim() || `music-engine exited with ${code}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch {
        reject(new Error("music-engine returned invalid JSON"));
      }
    });
  });
}

function tokenForArtifact(filePath: string): string {
  const existing = [...artifactTokens.entries()].find(([, value]) => value === filePath);
  if (existing) return existing[0];
  const token = crypto.randomBytes(24).toString("hex");
  artifactTokens.set(token, filePath);
  return token;
}

async function wavPeaks(filePath: string, targetBins = 1200): Promise<number[]> {
  const data = await readFile(filePath);
  if (data.length < 44 || data.toString("ascii", 0, 4) !== "RIFF" || data.toString("ascii", 8, 12) !== "WAVE") {
    return [];
  }
  let channels = 0;
  let bitsPerSample = 0;
  let audioFormat = 0;
  let pcmStart = -1;
  let pcmBytes = 0;
  for (let offset = 12; offset + 8 <= data.length; ) {
    const chunkId = data.toString("ascii", offset, offset + 4);
    const chunkSize = data.readUInt32LE(offset + 4);
    const content = offset + 8;
    if (content + chunkSize > data.length) break;
    if (chunkId === "fmt " && chunkSize >= 16) {
      audioFormat = data.readUInt16LE(content);
      channels = data.readUInt16LE(content + 2);
      bitsPerSample = data.readUInt16LE(content + 14);
    } else if (chunkId === "data") {
      pcmStart = content;
      pcmBytes = chunkSize;
      break;
    }
    offset = content + chunkSize + (chunkSize % 2);
  }
  if (audioFormat !== 1 || bitsPerSample !== 16 || channels < 1 || pcmStart < 0) return [];
  const frameBytes = channels * 2;
  const frames = Math.floor(pcmBytes / frameBytes);
  const bins = Math.max(1, Math.min(targetBins, frames));
  const framesPerBin = Math.ceil(frames / bins);
  const peaks = new Array<number>(bins).fill(0);
  for (let frame = 0; frame < frames; frame += 1) {
    let peak = 0;
    const frameOffset = pcmStart + frame * frameBytes;
    for (let channel = 0; channel < channels; channel += 1) {
      peak = Math.max(peak, Math.abs(data.readInt16LE(frameOffset + channel * 2)) / 32768);
    }
    const bin = Math.min(bins - 1, Math.floor(frame / framesPerBin));
    peaks[bin] = Math.max(peaks[bin], peak);
  }
  return peaks;
}

async function loadProjectState(): Promise<ProjectState> {
  if (!activeProjectPath) {
    return {
      status: "empty",
      projectPath: null,
      projectId: null,
      selectedCandidateId: null,
      candidates: [],
    };
  }
  try {
    const raw = (await runCli(["candidates", activeProjectPath])) as {
      projectId: string;
      selectedCandidateId: string | null;
      candidates: Array<Candidate & { path: string }>;
    };
    artifactTokens.clear();
    const candidates = await Promise.all(
      raw.candidates.map(async ({ path: filePath, ...candidate }) => ({
        ...candidate,
        audioUrl: candidate.artifactValid
          ? `music-artifact://artifact/${tokenForArtifact(filePath)}`
          : null,
        waveform: candidate.artifactValid ? await wavPeaks(filePath) : [],
      })),
    );
    return {
      status: "ready",
      projectPath: activeProjectPath,
      projectId: raw.projectId,
      selectedCandidateId: raw.selectedCandidateId,
      candidates,
    };
  } catch (error) {
    return {
      status: "error",
      projectPath: activeProjectPath,
      projectId: null,
      selectedCandidateId: null,
      candidates: [],
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

function requireCandidateId(candidateId: unknown): string {
  if (typeof candidateId !== "string" || !candidateIdPattern.test(candidateId)) {
    throw new Error("invalid candidate id");
  }
  return candidateId;
}

function requireActiveProject(): string {
  if (!activeProjectPath) throw new Error("open a project first");
  return activeProjectPath;
}

async function registerIpc(): Promise<void> {
  ipcMain.handle("music:choose-project", async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      title: "음악 프로젝트 열기",
      properties: ["openDirectory"],
    });
    if (result.canceled || !result.filePaths[0]) return loadProjectState();
    activeProjectPath = await validateProjectDirectory(result.filePaths[0]);
    await saveLastProject(activeProjectPath);
    return loadProjectState();
  });
  ipcMain.handle("music:get-state", () => loadProjectState());
  ipcMain.handle("music:refresh", () => loadProjectState());
  ipcMain.handle("music:select", async (_event, candidateId: unknown) => {
    await runCli(["select", requireActiveProject(), requireCandidateId(candidateId)]);
    return loadProjectState();
  });
  ipcMain.handle("music:undo-selection", async () => {
    await runCli(["undo-selection", requireActiveProject()]);
    return loadProjectState();
  });
  ipcMain.handle("music:review", async (_event, input: ReviewInput) => {
    const candidateId = requireCandidateId(input?.candidateId);
    const status = input?.status;
    if (!(["unreviewed", "listened", "approved", "rejected"] as const).includes(status)) {
      throw new Error("invalid review status");
    }
    const args = ["review", requireActiveProject(), candidateId, "--status", status];
    if (input.rating !== undefined) {
      if (!Number.isInteger(input.rating) || input.rating < 1 || input.rating > 5) {
        throw new Error("rating must be 1–5");
      }
      args.push("--rating", String(input.rating));
    }
    if (input.note) args.push("--note", input.note.slice(0, 2000));
    await runCli(args);
    return loadProjectState();
  });
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1260,
    height: 820,
    minWidth: 980,
    minHeight: 680,
    backgroundColor: "#0b0d12",
    title: "Local Music Engine",
    webPreferences: {
      preload: path.join(distRoot, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.loadFile(path.join(distRoot, "index.html"));
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
}

app.whenReady().then(async () => {
  protocol.handle("music-artifact", (request) => {
    const url = new URL(request.url);
    const token = url.pathname.replace(/^\//, "");
    const filePath = artifactTokens.get(token);
    if (!filePath) return new Response("Not found", { status: 404 });
    return stat(filePath).then(async (fileStat) => {
      const size = fileStat.size;
      const rangeHeader = request.headers.get("range");
      const match = rangeHeader?.match(/^bytes=(\d*)-(\d*)$/);
      if (!match) {
        const data = await readFile(filePath);
        return new Response(new Uint8Array(data), {
          status: 200,
          headers: {
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Content-Length": String(size),
            "Content-Type": "audio/wav",
          },
        });
      }
      const requestedStart = match[1] ? Number.parseInt(match[1], 10) : 0;
      const requestedEnd = match[2] ? Number.parseInt(match[2], 10) : size - 1;
      const start = Math.max(0, requestedStart);
      const end = Math.min(size - 1, requestedEnd);
      if (start >= size || end < start) {
        return new Response(null, {
          status: 416,
          headers: { "Content-Range": `bytes */${size}` },
        });
      }
      const data = (await readFile(filePath)).subarray(start, end + 1);
      return new Response(new Uint8Array(data), {
        status: 206,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Accept-Ranges": "bytes",
          "Content-Length": String(data.byteLength),
          "Content-Range": `bytes ${start}-${end}/${size}`,
          "Content-Type": "audio/wav",
        },
      });
    });
  });
  await registerIpc();
  const restored = await readLastProject();
  if (restored) {
    try {
      activeProjectPath = await validateProjectDirectory(restored);
    } catch {
      activeProjectPath = null;
    }
  }
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
