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
  ProductionCatalog,
  SongPlanResult,
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
import { aceGenerationArgs, aceReady, aceRepaintArgs, requirePlanAction } from "./generation-arguments.ts";
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
import { SongSession, type SongContext } from "./song-session.ts";
import { unloadAssistant } from "./assistant-model.ts";
import { parseProductionRules, productionPreview } from "../production-rules.ts";
import { parseRegenerateInput, parseSongPlanInput } from "../song-plan.ts";

const versionIdPattern = /^candidate_[a-f0-9]{32}$/;
const artifactIdPattern = /^artifact_[a-f0-9]{32}$/;
const strengths: Strength[] = ["light", "medium", "strong"];

export class Controller {
  engine: EngineManager;
  tasks: TaskRunner;
  private session = new SongSession(songIdFor);
  private rules: RuleHint[] | null = null;
  private productionCatalog: Promise<ProductionCatalog> | null = null;
  private startingGeneration = false;

  constructor(private window: () => BrowserWindow | null) {
    this.engine = new EngineManager(
      () => currentSettings().engineBaseUrl,
      (engine) => this.send({ type: "engine", engine }),
      () => currentSettings().musicModel,
      () => unloadAssistantModel(),
    );
    this.tasks = new TaskRunner({
      emit: (task) => this.send({ type: "task", task }),
      versionsChanged: (folder) => {
        if (folder === this.session.current()?.folder) void this.pushSong();
      },
      finished: (outcome, folder) => {
        this.send({ type: "task-finished", outcome });
        if (folder === this.session.current()?.folder) void this.pushSong();
        void this.pushSongs();
      },
    });
  }

  private send(event: MusicEvent): void {
    this.window()?.webContents.send("music:event", event);
  }

  private async pushSong(): Promise<void> {
    const context = this.session.current();
    if (!context) return;
    const song = await loadSong(context.folder);
    if (this.session.matches(context)) this.send({ type: "song", songId: context.songId, song });
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

  private async songState(folder = this.session.current()?.folder): Promise<SongState> {
    return folder ? loadSong(folder) : { status: "none", song: null };
  }

  private open(resolveFolder: () => Promise<string | null>, revision?: number): Promise<SongState | null> {
    return this.session.open(resolveFolder, loadSong, (folder) => updateAppState({ lastSongPath: folder }), revision);
  }

  private requireIdle(folder: string): void {
    if (this.tasks.isBusy(folder)) throw new Error("이 곡을 만드는 중이에요. 작업이 끝난 뒤 다시 시도하세요.");
  }

  private generationBusy(): boolean {
    return this.startingGeneration || this.tasks.busyFolder() !== null;
  }

  private async requireEngine(): Promise<void> {
    if (this.engine.handoff.active()) throw new Error("AI 도우미가 답하는 중이라 음악 엔진을 잠시 꺼 뒀어요. 끝나면 다시 켜져요.");
    if (aceReady(this.engine.snapshot()) && (this.engine.isReady() || this.engine.recentlyHealthy())) return;
    const status = await this.engine.check();
    if (aceReady(status)) return;
    if (status.state === "starting") throw new Error("음악 엔진을 켜는 중이에요. 준비되면 다시 시도하세요.");
    // A running engine with another model reports why; "off" would hide that.
    if (status.state === "failed" || status.state === "missing") throw new Error(status.detail);
    throw new Error("음악 엔진이 꺼져 있어요. 왼쪽 아래에서 엔진을 켜세요.");
  }

  // A local assistant LLM never shares memory with the music engine (see handoff.ts).
  private async withAssistant<T>(call: () => Promise<T>): Promise<T> {
    if (currentSettings().assistant.kind === "rules") return call();
    if (this.generationBusy()) {
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
    return aceGenerationArgs(folder, this.seeds(count), currentSettings());
  }

  private async startTask(spec: Omit<TaskSpec, "songId" | "songTitle">, context?: SongContext): Promise<SongState> {
    const state = await loadSong(spec.folder);
    if (context) this.session.assert(context);
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
    if (state.lastSongPath && !this.session.current()) {
      try {
        await this.open(() => validateSongFolder(state.lastSongPath!));
      } catch {
        // A missing recent song leaves the library open.
      }
    }
    const [songs, song, rules, productionCatalog] = await Promise.all([this.songs(), this.songState(), this.ruleHints(), this.productionRuleCatalog()]);
    return {
      settings,
      engine: this.engine.snapshot(),
      songs,
      song,
      task: this.tasks.snapshot(),
      rules,
      productionCatalog,
      info: { version: app.getVersion(), engineRoot, dataDir: app.getPath("userData") },
    };
  }

  private async ruleHints(): Promise<RuleHint[]> {
    if (!this.rules) {
      this.rules = (await runCli<{ rules: RuleHint[] }>(["assistant", "rules"]).catch(() => ({ rules: [] }))).rules;
    }
    return this.rules;
  }

  private productionRuleCatalog(): Promise<ProductionCatalog> {
    this.productionCatalog ??= runCli<ProductionCatalog>(["production-rules", "--engine", "ace-step"]).catch((error) => {
      this.productionCatalog = null;
      throw error;
    });
    return this.productionCatalog;
  }

  register(): void {
    const generationChannels = new Set(["create-song", "apply-plan", "generate-more", "regenerate-song", "resume"]);
    const handle = (channel: string, fn: (...args: any[]) => unknown) =>
      ipcMain.handle(`music:${channel}`, async (event, ...args) => {
        const window = this.window();
        if (!window || event.sender !== window.webContents || event.senderFrame !== event.sender.mainFrame) {
          throw new Error("앱의 작업 화면에서만 요청할 수 있어요.");
        }
        if (!generationChannels.has(channel)) return fn(...args);
        if (this.generationBusy()) throw new Error("진행 중인 만들기가 끝난 뒤 다시 시도하세요.");
        this.startingGeneration = true;
        try {
          return await fn(...args);
        } finally {
          this.startingGeneration = false;
        }
      });

    handle("bootstrap", () => this.bootstrap());
    handle("list-songs", () => this.songs());
    handle("open-song", (songId: unknown) => this.open(() => validateSongFolder(songPath(songId))));
    handle("open-song-folder", () => this.open(async () => {
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
      void this.pushSongs();
      return folder;
    }));
    handle("close-song", async (songId: unknown) => {
      this.session.require(songId);
      this.session.close();
      await updateAppState({ lastSongPath: null });
      return { status: "none", song: null } satisfies SongState;
    });
    handle("refresh-song", (songId: unknown) => this.songState(this.session.require(songId).folder));

    handle("create-song", async (input: CreateSongInput) => {
      const title = String(input?.title ?? "").trim().slice(0, 100);
      const style = String(input?.stylePrompt ?? "").trim();
      const lyrics = String(input?.lyrics ?? "").trim();
      const duration = Number(input?.durationSeconds);
      const versions = Number(input?.versions);
      const productionCatalog = await this.productionRuleCatalog();
      const productionRules = parseProductionRules(input?.productionRules, productionCatalog);
      if (!title) throw new Error("곡 제목을 적어 주세요.");
      const effective = productionPreview({ stylePrompt: style, instrumental: lyrics === "[Instrumental]", durationSeconds: duration, bpm: "", keyScale: "", timeSignature: "", productionRules }, productionCatalog);
      if (!effective.stylePrompt || style.length > 1500) throw new Error("스타일을 1,500자 안으로 적거나 제작 프리셋을 골라 주세요.");
      if (!lyrics || lyrics.length > 4096) throw new Error("가사를 4,096자 안으로 적어 주세요. 가사가 없으면 연주곡을 고르세요.");
      if (!Number.isFinite(duration) || duration < 10 || duration > 300) throw new Error("곡 길이는 10초에서 5분 사이여야 해요.");
      if (!Number.isInteger(versions) || versions < 1 || versions > 4) throw new Error("버전은 1~4개까지 만들 수 있어요.");
      if (input.bpm !== null && input.bpm !== undefined && (!Number.isInteger(input.bpm) || input.bpm < 30 || input.bpm > 300)) throw new Error("빠르기는 30~300 사이의 정수로 적어 주세요.");
      const opening = this.session.beginOpen();
      try {
        await this.requireEngine();
        const settings = currentSettings();
        const folder = await newSongFolder(settings.projectsDir, title);
        const args = ["init", folder, "--title", title, "--lyrics", lyrics, "--style", style, "--duration", String(Math.round(duration))];
        args.push("--production-rules-json", JSON.stringify(productionRules));
        if (input.bpm && Number.isInteger(input.bpm)) args.push("--bpm", String(input.bpm));
        if (input.keyScale?.trim()) args.push("--key", input.keyScale.trim().slice(0, 40));
        if (input.timeSignature?.trim()) args.push("--time-signature", input.timeSignature.trim().slice(0, 10));
        await runCli(args);
        await this.open(async () => folder, opening);
        return this.startTask({
          kind: "generate",
          folder,
          label: `버전 ${versions}개 만들기`,
          args: this.generationArgs(folder, versions),
          total: versions,
          jobKind: "candidate-batch",
        });
      } finally {
        this.session.cancelOpen(opening);
      }
    });

    handle("review", async (input: ReviewInput) => {
      const context = this.session.require(input?.songId);
      const { folder } = context;
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
      return this.songState(folder);
    });
    handle("set-final", async (songId: unknown, versionId: unknown) => {
      const context = this.session.require(songId);
      const { folder } = context;
      this.requireIdle(folder);
      await runCli(["select", folder, requireVersionId(versionId)]);
      void this.pushSongs();
      return this.songState(folder);
    });
    handle("undo-final", async (songId: unknown) => {
      const context = this.session.require(songId);
      const { folder } = context;
      this.requireIdle(folder);
      await runCli(["undo-selection", folder]);
      void this.pushSongs();
      return this.songState(folder);
    });
    handle("export-final", async (songId: unknown) => {
      const context = this.session.require(songId);
      const { folder } = context;
      this.requireIdle(folder);
      const state = await this.songState(folder);
      this.session.assert(context);
      const sourceId = state.song?.finalVersionId ?? state.song?.recommendedVersionId;
      if (!sourceId || !state.song) throw new Error("내보낼 버전이 아직 없어요. 곡 만들기가 끝나면 추천본을 저장할 수 있어요.");
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
      this.session.assert(context);
      this.requireIdle(folder);
      const output = result.filePath.toLowerCase().endsWith(".wav") ? result.filePath : `${result.filePath}.wav`;
      await runCli(["export", folder, "--candidate-id", sourceId, "--output", output]);
      await saveSettings({ lastExportDir: path.dirname(output) });
      void this.pushSongs();
      return { state: await this.songState(folder), path: output };
    });
    handle("revise", async (input: ReviseInput) => {
      const context = this.session.require(input?.songId);
      const { folder } = context;
      this.requireIdle(folder);
      const args = ["revise", folder];
      if (input?.title !== undefined) args.push("--title", String(input.title).slice(0, 100));
      if (input?.stylePrompt !== undefined) args.push("--style", String(input.stylePrompt).slice(0, 1500));
      if (input?.lyrics !== undefined) args.push("--lyrics", String(input.lyrics).slice(0, 4096));
      if (input?.durationSeconds !== undefined) args.push("--duration", String(Number(input.durationSeconds)));
      if (input?.bpm !== undefined) args.push("--bpm", String(input.bpm ?? 0));
      await runCli(args);
      void this.pushSongs();
      return this.songState(folder);
    });

    handle("draft", async (query: unknown, instrumental: unknown, duration: unknown, selection: unknown, vocalLanguage: unknown): Promise<DraftResult> => {
      const text = String(query ?? "").trim().slice(0, 1000);
      if (!text) throw new Error("어떤 곡인지 한 줄이라도 적어 주세요.");
      const productionRules = parseProductionRules(selection, await this.productionRuleCatalog());
      if (vocalLanguage !== undefined && vocalLanguage !== "ko" && vocalLanguage !== "en") throw new Error("가사 초안 언어가 올바르지 않아요.");
      const settings = currentSettings();
      // Rule drafts are local templates; neither draft path uses the music server.
      const seconds = Math.min(300, Math.max(10, Math.round(Number(duration) || settings.defaultDurationSeconds)));
      const args = ["draft", "--query", text, "--duration", String(seconds), ...this.assistantArgs()];
      args.push("--production-rules-json", JSON.stringify(productionRules), "--vocal-language", vocalLanguage === "en" ? "en" : "ko");
      if (instrumental === true) args.push("--instrumental");
      return this.withAssistant(() => runCli<DraftResult>(args));
    });
    handle("song-plan", async (input: unknown): Promise<SongPlanResult> => {
      const validated = parseSongPlanInput(input, await this.productionRuleCatalog());
      return runCli<SongPlanResult>(["song-plan", "--input-json", JSON.stringify(validated)]);
    });
    handle("regenerate-song", async (input: unknown) => {
      const validated = parseRegenerateInput(input);
      const context = this.session.require(validated.songId);
      this.requireIdle(context.folder);
      const state = await this.songState(context.folder);
      this.session.assert(context);
      const source = state.song?.versions.find((version) => version.id === validated.versionId);
      if (!source) throw new Error("새 버전에 사용할 가사와 편곡을 찾지 못했어요.");
      await this.requireEngine();
      this.session.assert(context);
      const args = [...this.generationArgs(context.folder, validated.versions), "--source-candidate-id", source.id];
      if (validated.stylePrompt !== undefined) args.push("--style", validated.stylePrompt);
      if (validated.lyrics !== undefined) args.push("--lyrics", validated.lyrics);
      return this.startTask({ kind: "generate", folder: context.folder, label: `가사·편곡으로 새 버전 ${validated.versions}개 만들기`, args, total: validated.versions, jobKind: "candidate-batch" }, context);
    });
    // Older app windows cannot turn a cover request into unrelated full generation.
    handle("cover-song", () => { throw new Error("원본 음원을 참조하는 커버는 앱에서 아직 만들 수 없어요. 가사·편곡으로 새 전체 버전을 만들 수 있어요."); });
    handle("plan", async (input: PlanInput): Promise<Plan> => {
      const context = this.session.require(input?.songId);
      const { folder } = context;
      const versionId = requireVersionId(input?.versionId);
      const feedback = String(input?.feedback ?? "").trim().slice(0, 2000);
      if (!feedback) throw new Error("무엇이 마음에 안 드는지 적어 주세요.");
      const strength = strengths.includes(input?.strength) ? input.strength : "medium";
      const versions = Math.min(4, Math.max(1, Math.round(Number(input?.versions) || 2)));
      const settings = currentSettings();
      const args = ["plan", folder, versionId, "--feedback", feedback, "--strength", strength, "--versions", String(versions)];
      if (input.range) {
        const { startSeconds, endSeconds } = input.range;
        if (!Number.isFinite(startSeconds) || !Number.isFinite(endSeconds) || startSeconds < 0 || endSeconds <= startSeconds) throw new Error("구간이 올바르지 않아요.");
        args.push("--start", startSeconds.toFixed(2), "--end", endSeconds.toFixed(2));
      }
      args.push("--engine", "ace-step");
      args.push(...this.assistantArgs());
      return this.withAssistant(() => runCli<Plan>(args));
    });
    handle("apply-plan", async (songId: unknown, plan: Plan, feedbackText: unknown) => {
      const context = this.session.require(songId);
      const { folder } = context;
      this.requireIdle(folder);
      const versionId = requireVersionId(plan?.candidateId);
      requirePlanAction(plan);
      const style = String(plan.stylePrompt ?? "").trim();
      if (!style || style.length > 1500) throw new Error("스타일 문장이 비어 있거나 너무 길어요.");
      const strength = strengths.includes(plan.strength) ? plan.strength : "medium";
      await this.requireEngine();
      this.session.assert(context);
      const settings = currentSettings();
      const text = String(feedbackText ?? "").trim().slice(0, 2000);
      const feedback = JSON.stringify({ text, candidateId: versionId, range: plan.range, plan });
      if (typeof plan.lyrics === "string" && plan.lyrics.trim()) {
        if (plan.lyrics.length > 4096) throw new Error("가사가 너무 길어요.");
      }
      if (plan.action === "repaint" && plan.range) {
        const range = plan.range;
        return this.startTask({
          kind: "repaint",
          folder,
          label: `${clock(range.startSeconds)}–${clock(range.endSeconds)} 다시 만들기`,
          args: [
            ...aceRepaintArgs(folder, versionId, range, this.seeds(1), settings),
            "--style", style,
            ...(typeof plan.lyrics === "string" && plan.lyrics.trim() ? ["--lyrics", plan.lyrics] : []),
            "--strength", strength,
            "--feedback-json", feedback,
          ],
          total: 1,
          jobKind: "repaint-candidate",
        }, context);
      }
      const count = Math.min(4, Math.max(1, Math.round(Number(plan.versions) || 2)));
      const args = [...this.generationArgs(folder, count), "--source-candidate-id", versionId, "--style", style, "--feedback-json", feedback];
      if (typeof plan.lyrics === "string" && plan.lyrics.trim()) args.push("--lyrics", plan.lyrics);
      if (plan.bpm && Number.isInteger(plan.bpm)) args.push("--bpm", String(plan.bpm));
      return this.startTask({ kind: "generate", folder, label: `새 버전 ${count}개 만들기`, args, total: count, jobKind: "candidate-batch" }, context);
    });
    handle("generate-more", async (songId: unknown, count: unknown) => {
      const context = this.session.require(songId);
      const { folder } = context;
      this.requireIdle(folder);
      await this.requireEngine();
      this.session.assert(context);
      const total = Math.min(4, Math.max(1, Math.round(Number(count) || 1)));
      return this.startTask({
        kind: "generate",
        folder,
        label: `버전 ${total}개 더 만들기`,
        args: this.generationArgs(folder, total),
        total,
        jobKind: "candidate-batch",
      }, context);
    });
    handle("resume", async (songId: unknown, jobId: unknown) => {
      const context = this.session.require(songId);
      const { folder } = context;
      this.requireIdle(folder);
      await this.requireEngine();
      this.session.assert(context);
      const state = await this.songState(folder);
      const target = state.song?.jobs.find((job) => job.jobId === jobId && ["candidate-batch", "cover-batch"].includes(job.kind));
      if (!target?.canResume || target.kind !== "candidate-batch") throw new Error("이어서 만들 수 있는 작업을 골라 주세요. 곡을 새로 열면 상태를 확인할 수 있어요.");
      return this.startTask({
        kind: "resume",
        folder,
        label: "멈춘 생성 이어서 만들기",
        args: ["resume", folder, "--engine", "ace-step", "--job-id", target.jobId, "--base-url", currentSettings().engineBaseUrl],
        total: target.seeds?.length ?? 1,
        jobKind: "candidate-batch",
      }, context);
    });
    handle("cancel-task", (songId: unknown, startedAt: unknown) => {
      const task = this.tasks.snapshot();
      if (!task || task.songId !== songId || task.startedAt !== startedAt) throw new Error("진행 중인 작업이 바뀌었어요. 현재 작업에서 다시 시도하세요.");
      return this.tasks.cancel();
    });

    handle("reveal", async (target: RevealTarget) => {
      if (target?.kind === "version") {
        shell.showItemInFolder(versionFilePath(requireVersionId(target.versionId)));
      } else if (target?.kind === "song") {
        const folder = songPath(target.songId);
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
        "engineBaseUrl", "engineAutoStart", "musicModel", "defaultVersions",
        "defaultDurationSeconds", "feedbackStrength", "assistant",
      ] as const) {
        if (partial && key in partial) (allowed as Record<string, unknown>)[key] = partial[key];
      }
      let next: Settings;
      if (allowed.musicModel !== undefined && allowed.musicModel !== before.musicModel && (this.tasks.snapshot() || this.startingGeneration)) {
        throw new Error("곡 만들기가 끝난 뒤 음악 모델을 바꿔 주세요.");
      }
      if (allowed.engineBaseUrl !== undefined && requireLoopbackUrl(allowed.engineBaseUrl, "음악 엔진") !== before.engineBaseUrl) {
        if (this.tasks.snapshot() || this.startingGeneration) throw new Error("곡 만들기가 끝나고 음악 엔진을 끈 뒤 주소를 바꿔 주세요.");
        next = await this.engine.withConnectionChange(allowed.engineBaseUrl, () => saveSettings(allowed));
      } else {
        next = await saveSettings(allowed);
      }
      if (next.engineBaseUrl !== before.engineBaseUrl || next.musicModel !== before.musicModel) {
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
    handle("engine-stop", () => {
      if (this.generationBusy()) throw new Error("곡을 준비하거나 만드는 중에는 엔진을 끌 수 없어요. 작업 화면의 ‘취소하기’를 먼저 눌러 주세요.");
      return this.engine.stop();
    });
    handle("engine-check", () => this.engine.check());
  }
}

function clock(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function requireVersionId(value: unknown): string {
  if (typeof value !== "string" || !versionIdPattern.test(value)) throw new Error("알 수 없는 버전이에요.");
  return value;
}

// Ollama keeps a model resident for minutes after its last answer (the engine's own
// calls pass keep_alive 0). Unload the assistant model before the engine loads its own.
async function unloadAssistantModel(): Promise<void> {
  await unloadAssistant(currentSettings().assistant);
}
