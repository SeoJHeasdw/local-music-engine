import { app, dialog, ipcMain, shell, type BrowserWindow } from "electron";
import crypto from "node:crypto";
import { access, mkdir } from "node:fs/promises";
import path from "node:path";
import type {
  AssistantKind,
  Bootstrap,
  CreateSongInput,
  DraftResult,
  MusicEvent,
  Plan,
  PlanInput,
  RevealTarget,
  ReviewInput,
  ReviseInput,
  RuleHint,
  Settings,
  SongState,
  Strength,
} from "../shared.ts";
import { runCli } from "./cli.ts";
import { EngineManager } from "./engine.ts";
import { requireLoopbackUrl } from "./files.ts";
import { engineRoot } from "./paths.ts";
import { currentSettings, loadAppState, loadSettings, saveSettings, updateAppState } from "./settings.ts";
import {
  exportFilePath,
  listSongs,
  loadSong,
  newSongFolder,
  songIdFor,
  songPath,
  validateSongFolder,
  versionFilePath,
} from "./songs.ts";
import { TaskRunner, type TaskSpec } from "./tasks.ts";

const versionIdPattern = /^candidate_[a-f0-9]{32}$/;
const artifactIdPattern = /^artifact_[a-f0-9]{32}$/;
const strengths: Strength[] = ["light", "medium", "strong"];

export class Controller {
  engine: EngineManager;
  tasks: TaskRunner;
  private openFolder: string | null = null;
  private rules: RuleHint[] | null = null;
  private startingGeneration = false;

  constructor(private window: () => BrowserWindow | null) {
    this.engine = new EngineManager(
      () => currentSettings().aceBaseUrl,
      (engine) => this.send({ type: "engine", engine }),
      () => ({ dit: currentSettings().ditModel, lm: currentSettings().lmModel }),
      () => unloadAssistantModel(),
    );
    this.tasks = new TaskRunner({
      emit: (task) => this.send({ type: "task", task }),
      versionsChanged: (folder) => {
        if (folder === this.openFolder) void this.pushSong();
      },
      finished: (outcome, folder) => {
        this.send({ type: "task-finished", outcome });
        if (folder === this.openFolder) void this.pushSong();
        void this.pushSongs();
      },
    });
  }

  private send(event: MusicEvent): void {
    this.window()?.webContents.send("music:event", event);
  }

  private async pushSong(): Promise<void> {
    if (this.openFolder) this.send({ type: "song", song: await this.songState() });
  }

  private async pushSongs(): Promise<void> {
    try {
      this.send({ type: "songs", songs: await this.songs() });
    } catch {
      // The library view refetches on its own; a failed background refresh is not fatal.
    }
  }

  private async songs() {
    const settings = currentSettings();
    const state = await loadAppState();
    const busy = this.tasks.busyFolder();
    return listSongs(settings.projectsDir, state.extraSongPaths, new Set(busy ? [busy] : []));
  }

  private async songState(): Promise<SongState> {
    if (!this.openFolder) return { status: "none", song: null };
    return loadSong(this.openFolder);
  }

  private async open(folder: string): Promise<SongState> {
    this.openFolder = folder;
    songIdFor(folder);
    await updateAppState({ lastSongPath: folder });
    return this.songState();
  }

  private requireFolder(): string {
    if (!this.openFolder) throw new Error("먼저 곡을 여세요.");
    return this.openFolder;
  }

  private requireIdle(folder: string): void {
    if (this.tasks.isBusy(folder)) throw new Error("이 곡을 만드는 중이에요. 작업이 끝난 뒤 다시 시도하세요.");
  }

  private async requireEngine(): Promise<void> {
    if (this.engine.handoff.active()) throw new Error("AI 도우미가 답하는 중이라 음악 엔진을 잠시 꺼 뒀어요. 끝나면 다시 켜져요.");
    if (this.engine.isReady() || this.engine.recentlyHealthy()) return;
    const status = await this.engine.check();
    if (status.state === "ready" || status.state === "external" || this.engine.recentlyHealthy()) return;
    if (status.state === "starting") throw new Error("음악 엔진을 켜는 중이에요. 준비되면 다시 시도하세요.");
    throw new Error("음악 엔진이 꺼져 있어요. 왼쪽 아래에서 엔진을 켜세요.");
  }

  // A local assistant LLM never shares memory with the music engine (see handoff.ts).
  private async withAssistant<T>(call: () => Promise<T>): Promise<T> {
    if (currentSettings().assistant.kind === "rules") return call();
    if (this.tasks.busyFolder()) {
      throw new Error("곡을 만드는 동안에는 AI 도우미를 쓸 수 없어요. 음악 엔진과 도우미 모델을 함께 올리면 메모리가 부족해요. 끝난 뒤 다시 시도하세요.");
    }
    return this.engine.handoff.withEngineStopped(call);
  }

  private assistantArgs(): string[] {
    const assistant = currentSettings().assistant;
    if (assistant.kind === "rules") return ["--assistant", "rules"];
    return ["--assistant", assistant.kind, "--assistant-url", assistant.baseUrl, "--assistant-model", assistant.model];
  }

  private seeds(count: number): string {
    const seeds = new Set<number>();
    while (seeds.size < count) seeds.add(crypto.randomInt(1, 2_147_483_647));
    return [...seeds].join(",");
  }

  private generationArgs(folder: string, count: number): string[] {
    const settings = currentSettings();
    return [
      "generate",
      folder,
      "--seeds",
      this.seeds(count),
      "--base-url",
      settings.aceBaseUrl,
      "--model",
      settings.ditModel,
      "--lm-model",
      settings.lmModel,
    ];
  }

  private async startTask(spec: Omit<TaskSpec, "songId" | "songTitle">): Promise<SongState> {
    const state = await this.songState();
    const song = state.song;
    this.tasks.start({
      ...spec,
      songId: songIdFor(spec.folder),
      songTitle: song?.title ?? path.basename(spec.folder),
    });
    void this.pushSongs();
    return state;
  }

  async bootstrap(): Promise<Bootstrap> {
    const settings = await loadSettings();
    const state = await loadAppState();
    if (state.lastSongPath && !this.openFolder) {
      try {
        this.openFolder = await validateSongFolder(state.lastSongPath);
      } catch {
        this.openFolder = null;
      }
    }
    const [songs, song, rules] = await Promise.all([this.songs(), this.songState(), this.ruleHints()]);
    return {
      settings,
      engine: this.engine.snapshot(),
      songs,
      song,
      task: this.tasks.snapshot(),
      rules,
      info: { version: app.getVersion(), engineRoot, dataDir: app.getPath("userData") },
    };
  }

  private async ruleHints(): Promise<RuleHint[]> {
    if (!this.rules) {
      this.rules = (await runCli<{ rules: RuleHint[] }>(["assistant", "rules"]).catch(() => ({ rules: [] }))).rules;
    }
    return this.rules;
  }

  register(): void {
    const generationChannels = new Set(["create-song", "apply-plan", "generate-more", "resume"]);
    const handle = (channel: string, fn: (...args: any[]) => unknown) =>
      ipcMain.handle(`music:${channel}`, async (event, ...args) => {
        const window = this.window();
        if (!window || event.sender !== window.webContents || event.senderFrame !== event.sender.mainFrame) {
          throw new Error("앱의 작업 화면에서만 요청할 수 있어요.");
        }
        if (!generationChannels.has(channel)) return fn(...args);
        if (this.startingGeneration || this.tasks.snapshot()) throw new Error("진행 중인 만들기가 끝난 뒤 다시 시도하세요.");
        this.startingGeneration = true;
        try {
          return await fn(...args);
        } finally {
          this.startingGeneration = false;
        }
      });

    handle("bootstrap", () => this.bootstrap());
    handle("list-songs", () => this.songs());
    handle("open-song", async (songId: unknown) => this.open(await validateSongFolder(songPath(songId))));
    handle("open-song-folder", async () => {
      const window = this.window();
      const result = window
        ? await dialog.showOpenDialog(window, { title: "곡 폴더 열기", properties: ["openDirectory"] })
        : { canceled: true, filePaths: [] };
      if (result.canceled || !result.filePaths[0]) return null;
      const folder = await validateSongFolder(result.filePaths[0]);
      const settings = currentSettings();
      if (path.dirname(folder) !== path.resolve(settings.projectsDir)) {
        const state = await loadAppState();
        await updateAppState({ extraSongPaths: [folder, ...state.extraSongPaths.filter((item) => item !== folder)] });
      }
      const opened = await this.open(folder);
      void this.pushSongs();
      return opened;
    });
    handle("close-song", async () => {
      this.openFolder = null;
      await updateAppState({ lastSongPath: null });
      return { status: "none", song: null } satisfies SongState;
    });
    handle("refresh-song", () => this.songState());

    handle("create-song", async (input: CreateSongInput) => {
      const title = String(input?.title ?? "").trim().slice(0, 100);
      const style = String(input?.stylePrompt ?? "").trim();
      const lyrics = String(input?.lyrics ?? "").trim();
      const duration = Number(input?.durationSeconds);
      const versions = Number(input?.versions);
      if (!title) throw new Error("곡 제목을 적어 주세요.");
      if (!style || style.length > 1500) throw new Error("스타일을 1,500자 안으로 적어 주세요.");
      if (!lyrics || lyrics.length > 4096) throw new Error("가사를 4,096자 안으로 적어 주세요. 가사가 없으면 연주곡을 고르세요.");
      if (!Number.isFinite(duration) || duration < 10 || duration > 600) throw new Error("곡 길이는 10초에서 10분 사이여야 해요.");
      if (!Number.isInteger(versions) || versions < 1 || versions > 4) throw new Error("버전은 1~4개까지 만들 수 있어요.");
      await this.requireEngine();
      const settings = currentSettings();
      const folder = await newSongFolder(settings.projectsDir, title);
      const args = ["init", folder, "--title", title, "--lyrics", lyrics, "--style", style, "--duration", String(Math.round(duration))];
      if (input.bpm && Number.isInteger(input.bpm)) args.push("--bpm", String(input.bpm));
      if (input.keyScale?.trim()) args.push("--key", input.keyScale.trim().slice(0, 40));
      if (input.timeSignature?.trim()) args.push("--time-signature", input.timeSignature.trim().slice(0, 10));
      await runCli(args);
      await this.open(folder);
      return this.startTask({
        kind: "generate",
        folder,
        label: `버전 ${versions}개 만들기`,
        args: this.generationArgs(folder, versions),
        total: versions,
        jobKind: "candidate-batch",
      });
    });

    handle("review", async (input: ReviewInput) => {
      const folder = this.requireFolder();
      const versionId = requireVersionId(input?.versionId);
      const status = input?.status;
      if (!(["unreviewed", "listened", "approved", "rejected"] as const).includes(status)) throw new Error("알 수 없는 평가예요.");
      const rating = input.rating ?? null;
      if (rating !== null && (!Number.isInteger(rating) || rating < 1 || rating > 5)) throw new Error("별점은 1~5 사이예요.");
      const note = input.note?.trim().slice(0, 2000) || undefined;
      const args = ["review", folder, versionId, "--status", status];
      if (rating) args.push("--rating", String(rating));
      if (note) args.push("--note", note);
      await runCli(args);
      void this.pushSongs();
      return this.songState();
    });
    handle("set-final", async (versionId: unknown) => {
      const folder = this.requireFolder();
      this.requireIdle(folder);
      await runCli(["select", folder, requireVersionId(versionId)]);
      void this.pushSongs();
      return this.songState();
    });
    handle("undo-final", async () => {
      const folder = this.requireFolder();
      this.requireIdle(folder);
      await runCli(["undo-selection", folder]);
      void this.pushSongs();
      return this.songState();
    });
    handle("export-final", async () => {
      const folder = this.requireFolder();
      this.requireIdle(folder);
      const state = await this.songState();
      if (!state.song?.finalVersionId) throw new Error("먼저 최종본을 지정하세요.");
      const settings = currentSettings();
      const directory = settings.lastExportDir ?? app.getPath("music");
      const safeTitle = state.song.title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "").trim() || "song";
      const window = this.window();
      if (!window) return null;
      const result = await dialog.showSaveDialog(window, {
        title: "최종본 WAV로 내보내기",
        defaultPath: path.join(directory, `${safeTitle}.wav`),
        filters: [{ name: "WAV 오디오", extensions: ["wav"] }],
      });
      if (result.canceled || !result.filePath) return null;
      const output = result.filePath.toLowerCase().endsWith(".wav") ? result.filePath : `${result.filePath}.wav`;
      await runCli(["export", folder, "--output", output]);
      await saveSettings({ lastExportDir: path.dirname(output) });
      void this.pushSongs();
      return { state: await this.songState(), path: output };
    });
    handle("revise", async (input: ReviseInput) => {
      const folder = this.requireFolder();
      this.requireIdle(folder);
      const args = ["revise", folder];
      if (input?.title !== undefined) args.push("--title", String(input.title).slice(0, 100));
      if (input?.stylePrompt !== undefined) args.push("--style", String(input.stylePrompt).slice(0, 1500));
      if (input?.lyrics !== undefined) args.push("--lyrics", String(input.lyrics).slice(0, 4096));
      if (input?.durationSeconds !== undefined) args.push("--duration", String(Number(input.durationSeconds)));
      if (input?.bpm !== undefined) args.push("--bpm", String(input.bpm ?? 0));
      await runCli(args);
      void this.pushSongs();
      return this.songState();
    });

    handle("draft", async (query: unknown, instrumental: unknown, duration: unknown): Promise<DraftResult> => {
      const text = String(query ?? "").trim().slice(0, 1000);
      if (!text) throw new Error("어떤 곡인지 한 줄이라도 적어 주세요.");
      const settings = currentSettings();
      // The LLM writes drafts without the music engine; only the rules path needs ACE.
      if (settings.assistant.kind === "rules") await this.requireEngine();
      const seconds = Math.min(600, Math.max(10, Math.round(Number(duration) || settings.defaultDurationSeconds)));
      const args = ["draft", "--query", text, "--duration", String(seconds), "--base-url", settings.aceBaseUrl, ...this.assistantArgs()];
      if (instrumental === true) args.push("--instrumental");
      return this.withAssistant(() => runCli<DraftResult>(args));
    });
    handle("plan", async (input: PlanInput): Promise<Plan> => {
      const folder = this.requireFolder();
      const versionId = requireVersionId(input?.versionId);
      const feedback = String(input?.feedback ?? "").trim().slice(0, 2000);
      if (!feedback) throw new Error("무엇이 마음에 안 드는지 적어 주세요.");
      const strength = strengths.includes(input?.strength) ? input.strength : "medium";
      const versions = Math.min(4, Math.max(1, Math.round(Number(input?.versions) || 2)));
      const settings = currentSettings();
      const args = ["plan", folder, versionId, "--feedback", feedback, "--strength", strength, "--versions", String(versions)];
      if (input.range) {
        const { startSeconds, endSeconds } = input.range;
        if (!Number.isFinite(startSeconds) || !Number.isFinite(endSeconds) || endSeconds <= startSeconds) throw new Error("구간이 올바르지 않아요.");
        args.push("--start", startSeconds.toFixed(2), "--end", endSeconds.toFixed(2));
      }
      args.push(...this.assistantArgs());
      return this.withAssistant(() => runCli<Plan>(args));
    });
    handle("apply-plan", async (plan: Plan, feedbackText: unknown) => {
      const folder = this.requireFolder();
      this.requireIdle(folder);
      const versionId = requireVersionId(plan?.candidateId);
      if (plan.action !== "repaint" && plan.action !== "regenerate") throw new Error("알 수 없는 수정 방식이에요.");
      const style = String(plan.stylePrompt ?? "").trim();
      if (!style || style.length > 1500) throw new Error("스타일 문장이 비어 있거나 너무 길어요.");
      const strength = strengths.includes(plan.strength) ? plan.strength : "medium";
      await this.requireEngine();
      const settings = currentSettings();
      const text = String(feedbackText ?? "").trim().slice(0, 2000);
      const feedback = JSON.stringify({ text, candidateId: versionId, range: plan.range, plan });
      if (typeof plan.lyrics === "string" && plan.lyrics.trim()) {
        if (plan.lyrics.length > 4096) throw new Error("가사가 너무 길어요.");
      }
      if (plan.action === "repaint") {
        const range = plan.range;
        if (!range || !(range.endSeconds > range.startSeconds) || range.startSeconds < 0) throw new Error("고칠 구간을 파형에서 골라 주세요.");
        return this.startTask({
          kind: "repaint",
          folder,
          label: `${clock(range.startSeconds)}–${clock(range.endSeconds)} 다시 만들기`,
          args: [
            "repaint", folder,
            "--candidate-id", versionId,
            "--start", range.startSeconds.toFixed(2),
            "--end", range.endSeconds.toFixed(2),
            "--seed", this.seeds(1),
            "--style", style,
            ...(typeof plan.lyrics === "string" && plan.lyrics.trim() ? ["--lyrics", plan.lyrics] : []),
            "--strength", strength,
            "--feedback-json", feedback,
            "--base-url", settings.aceBaseUrl,
          ],
          total: 1,
          jobKind: "repaint-candidate",
        });
      }
      const count = Math.min(4, Math.max(1, Math.round(Number(plan.versions) || 2)));
      const args = [...this.generationArgs(folder, count), "--source-candidate-id", versionId, "--style", style, "--feedback-json", feedback];
      if (typeof plan.lyrics === "string" && plan.lyrics.trim()) args.push("--lyrics", plan.lyrics);
      if (plan.bpm && Number.isInteger(plan.bpm)) args.push("--bpm", String(plan.bpm));
      return this.startTask({ kind: "generate", folder, label: `새 버전 ${count}개 만들기`, args, total: count, jobKind: "candidate-batch" });
    });
    handle("generate-more", async (count: unknown) => {
      const folder = this.requireFolder();
      this.requireIdle(folder);
      await this.requireEngine();
      const total = Math.min(4, Math.max(1, Math.round(Number(count) || 1)));
      return this.startTask({
        kind: "generate",
        folder,
        label: `버전 ${total}개 더 만들기`,
        args: this.generationArgs(folder, total),
        total,
        jobKind: "candidate-batch",
      });
    });
    handle("resume", async (jobId: unknown) => {
      const folder = this.requireFolder();
      this.requireIdle(folder);
      await this.requireEngine();
      const state = await this.songState();
      const target = state.song?.jobs.find((job) => job.jobId === jobId && job.kind === "candidate-batch");
      if (!target?.canResume) throw new Error("이어서 만들 수 있는 작업을 골라 주세요. 곡을 새로 열면 상태를 확인할 수 있어요.");
      return this.startTask({
        kind: "resume",
        folder,
        label: "멈춘 생성 이어서 만들기",
        args: ["resume", folder, "--job-id", target.jobId, "--base-url", currentSettings().aceBaseUrl],
        total: target.seeds?.length ?? 1,
        jobKind: "candidate-batch",
      });
    });
    handle("cancel-task", () => this.tasks.cancel());

    handle("reveal", async (target: RevealTarget) => {
      if (target?.kind === "version") {
        shell.showItemInFolder(versionFilePath(requireVersionId(target.versionId)));
      } else if (target?.kind === "song") {
        const folder = target.songId ? songPath(target.songId) : this.requireFolder();
        shell.showItemInFolder(path.join(folder, "project.json"));
      } else if (target?.kind === "export") {
        if (!artifactIdPattern.test(String(target.artifactId))) throw new Error("알 수 없는 파일이에요.");
        shell.showItemInFolder(exportFilePath(target.artifactId));
      } else if (target?.kind === "songs-dir") {
        const directory = currentSettings().projectsDir;
        await mkdir(directory, { recursive: true });
        await shell.openPath(directory);
      } else if (target?.kind === "engine-log") {
        const file = this.engine.logFile();
        try {
          await access(file);
          shell.showItemInFolder(file);
        } catch {
          throw new Error("아직 엔진 기록이 없어요. 앱에서 엔진을 켜면 기록이 남아요.");
        }
      }
    });

    handle("save-settings", async (partial: Partial<Settings>) => {
      const before = currentSettings();
      const allowed: Partial<Settings> = {};
      for (const key of [
        "aceBaseUrl", "aceAutoStart", "ditModel", "lmModel", "defaultVersions",
        "defaultDurationSeconds", "repaintStrength", "assistant",
      ] as const) {
        if (partial && key in partial) (allowed as Record<string, unknown>)[key] = partial[key];
      }
      const next = await saveSettings(allowed);
      if (next.aceBaseUrl !== before.aceBaseUrl || next.ditModel !== before.ditModel || next.lmModel !== before.lmModel) {
        void this.engine.check();
      }
      return next;
    });
    handle("pick-projects-dir", async () => {
      const window = this.window();
      if (!window) return currentSettings();
      const result = await dialog.showOpenDialog(window, {
        title: "곡을 저장할 폴더",
        defaultPath: currentSettings().projectsDir,
        properties: ["openDirectory", "createDirectory"],
      });
      if (result.canceled || !result.filePaths[0]) return currentSettings();
      const next = await saveSettings({ projectsDir: result.filePaths[0] });
      void this.pushSongs();
      return next;
    });
    handle("assistant-models", async (kind: AssistantKind, baseUrl: unknown) => {
      if (kind !== "ollama" && kind !== "openai") return [];
      const url = requireLoopbackUrl(baseUrl, "도우미 LLM");
      const result = await runCli<{ models: string[] }>(["assistant", "models", "--assistant", kind, "--assistant-url", url]);
      return result.models;
    });
    handle("engine-start", () => this.engine.start());
    handle("engine-stop", () => this.engine.stop());
    handle("engine-check", () => this.engine.check());
  }
}

function requireVersionId(value: unknown): string {
  if (typeof value !== "string" || !versionIdPattern.test(value)) throw new Error("알 수 없는 버전이에요.");
  return value;
}

function clock(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

// Ollama keeps a model resident for minutes after its last answer (the engine's own
// calls pass keep_alive 0). Unload the assistant model before the engine loads its own.
async function unloadAssistantModel(): Promise<void> {
  const assistant = currentSettings().assistant;
  if (assistant.kind !== "ollama" || !assistant.model) return;
  const base = requireLoopbackUrl(assistant.baseUrl, "AI 도우미").replace(/\/$/, "");
  const loaded = await fetch(`${base}/api/ps`, { signal: AbortSignal.timeout(3000), redirect: "error" });
  const body = (await loaded.json()) as { models?: { name?: string; model?: string }[] };
  if (!body.models?.some((item) => item.name === assistant.model || item.model === assistant.model)) return;
  await fetch(`${base}/api/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: assistant.model, keep_alive: 0 }),
    signal: AbortSignal.timeout(15000),
    redirect: "error",
  });
}
