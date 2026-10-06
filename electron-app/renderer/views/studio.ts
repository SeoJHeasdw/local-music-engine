import type { Plan, ReviewStatus, Song, Strength, Version } from "../../shared.ts";
import { activeVersion, api, applySongForContext, engineReady, go, refreshSong, songBusy, startEngine } from "../actions.ts";
import { byId, h, icon, mount, type Child } from "../dom.ts";
import {
  clock,
  elapsed,
  findingCopy,
  hasTag,
  lengthLabel,
  rangeLabel,
  relativeTime,
  reviewCopy,
  strengthCopy,
  strengthFromValue,
  tagDiff,
  toggleTag,
  versionTree,
  versionSourceParent,
  type VersionNode,
} from "../format.ts";
import { Player } from "../player.ts";
import { get, isPlanTicket, isSongTicket, planTicket, set, songTicket, type InspectorTab, type State } from "../store.ts";
import { confirmDialog, errorText, menu, toast, withBusy } from "../ui.ts";
import { audioQualityText, isAutomaticAttempt, lyricQualityText, processingText, qualityLabel, qualityRetryReason } from "../quality.ts";
import { songPlanView } from "../song-plan-view.ts";
import { comparisonChoices, comparisonVersion } from "../comparison.ts";
import { INSTRUMENTS, applyInstrumentChanges, hasInstrument } from "../instruments.ts";

// The song page. Top to bottom: what is playing, which versions exist, what made the
// selected one. The right column is the one place to ask for a change.

type Els = {
  head: HTMLElement;
  banner: HTMLElement;
  nowPlaying: HTMLElement;
  canvas: HTMLCanvasElement;
  now: HTMLElement;
  total: HTMLElement;
  play: HTMLButtonElement;
  loop: HTMLButtonElement;
  selectionBar: HTMLElement;
  zoom: HTMLElement;
  overview: HTMLCanvasElement;
  compare: HTMLElement;
  versions: HTMLElement;
  tabs: HTMLElement;
  info: HTMLElement;
  fixTarget: HTMLElement;
  fixScope: HTMLElement;
  fixChips: HTMLElement;
  fixInstruments: HTMLDetailsElement;
  fixArea: HTMLTextAreaElement;
  fixOptions: HTMLElement;
  planButton: HTMLButtonElement;
  assistantLabel: HTMLButtonElement;
  planBox: HTMLElement;
  history: HTMLElement;
  noteArea: HTMLTextAreaElement;
  lyricsArea: HTMLTextAreaElement;
};

let els: Els | null = null;
let player: Player | null = null;
let treeCache: { song: Song; tree: ReturnType<typeof versionTree> } | null = null;
let showAllChips = false;
// Lyrics being rewritten in the 가사 tab. Kept per version so switching tabs keeps the text.
let lyricsEdit: { versionId: string; base: string } | null = null;
let lyricsActions: HTMLButtonElement[] = [];
// Instruments to add or drop on the next fix, for the version they were chosen on.
let instrumentChanges = new Map<string, "add" | "remove">();
let instrumentVersion: string | null = null;

const QUICK_RULES = ["diction", "vocal-forward", "ending", "transition", "calmer", "energetic", "drums-soft", "clean", "brighter", "emotional", "chorus", "faster", "slower", "variety"];
const CHIPS_SHOWN = 6;

function tree() {
  const song = get().song.song;
  if (!song) return null;
  if (treeCache?.song !== song) treeCache = { song, tree: versionTree(song) };
  return treeCache.tree;
}

function node(id: string | null | undefined): VersionNode | undefined {
  return id ? tree()?.byId.get(id) : undefined;
}

function reveal(target: Parameters<typeof api.reveal>[0]) {
  void api.reveal(target).catch((error) => toast(errorText(error), { tone: "error" }));
}

// ---------------------------------------------------------------------------
// Public controls (keyboard shortcuts call these)
// ---------------------------------------------------------------------------

export function togglePlay(): void {
  player?.toggle();
}

export function nudge(seconds: number): void {
  player?.nudge(seconds);
}

export function stepVersion(direction: 1 | -1): void {
  const history = Boolean(activeVersion() && isAutomaticAttempt(activeVersion()!));
  const flat = (tree()?.flat ?? []).filter((item) => isAutomaticAttempt(item.version) === history);
  const index = flat.findIndex((item) => item.version.id === get().activeVersionId);
  const next = flat[Math.min(flat.length - 1, Math.max(0, index + direction))];
  if (next) selectVersion(next.version.id);
}

export function toggleCompare(): void {
  const version = activeVersion();
  const state = get();
  const song = state.song.song;
  if (song && version && comparisonVersion(song, version, state.comparisonVersionId)) set({ listenToParent: !state.listenToParent });
}

export function zoomWave(direction: 1 | -1 | 0): void {
  if (!player) return;
  if (direction === 0) player.showWhole();
  else player.zoomBy(direction > 0 ? 2 : 0.5);
}

export function toggleLoop(): void {
  if (get().selection) set({ loop: !get().loop });
}

export function clearSelection(): boolean {
  if (!get().selection) return false;
  set({ selection: null, scope: "song", loop: false });
  return true;
}

export function focusFeedback(): void {
  els?.fixArea.focus();
}

function selectVersion(id: string, play = false): void {
  if (id !== get().activeVersionId) set({ activeVersionId: id, listenToParent: false, plan: null });
  if (play && player && !player.playing) player.toggle();
}

// ---------------------------------------------------------------------------
// Build once
// ---------------------------------------------------------------------------

function build(): void {
  const root = byId("view-studio");
  const canvas = h("canvas", { class: "wave", "aria-label": "파형. 끌어서 구간을 고르고, 클릭해서 위치를 옮겨요." });
  const now = h("span", { class: "mono clock-now" }, "0:00.0");
  const total = h("span", { class: "mono clock-total" }, "0:00.0");
  const play = h("button", { type: "button", class: "play-button", "aria-label": "재생", "data-tip": "재생 / 멈춤 (Space)", onClick: () => togglePlay() }, icon("play", 24));
  const loop = h("button", { type: "button", class: "button ghost small", "aria-pressed": "false", "data-tip": "고른 구간만 반복해서 들어요 (L)", onClick: () => toggleLoop() }, icon("loop", 14), "반복");
  const volume = h("input", {
    type: "range",
    class: "volume",
    min: "0",
    max: "1",
    step: "0.01",
    "aria-label": "음량",
    value: localStorageGet("volume", "0.9"),
    onInput: (event: Event) => {
      const value = (event.target as HTMLInputElement).value;
      if (player) player.audio.volume = Number(value);
      localStorageSet("volume", value);
    },
  });
  const fixArea = h("textarea", {
    class: "input fix-input",
    rows: 4,
    maxlength: 2000,
    placeholder: "들은 그대로 적어 주세요.\n예: 후렴 가사가 잘 안 들리고 드럼이 너무 세요",
    onInput: (event: Event) => set({ feedback: (event.target as HTMLTextAreaElement).value }),
    onKeydown: (event: KeyboardEvent) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        void requestPlan();
      }
    },
  });
  const noteArea = h("textarea", { class: "input", rows: 2, maxlength: 2000, placeholder: "기억해 둘 점. 예: 1:12 ‘기억해’가 ‘기억개’로 들림" });
  const lyricsArea = h("textarea", { class: "input lyrics-input", rows: 16, maxlength: 4096, "aria-label": "고칠 가사", onInput: () => syncLyricsActions() });
  const planButton = h("button", { type: "button", class: "button primary block", onClick: () => void requestPlan() }, icon("wand", 16), "수정안 보기");
  const refs: Els = {
    head: h("header", { class: "studio-head" }),
    banner: h("div", { class: "studio-banner" }),
    nowPlaying: h("div", { class: "now-playing" }),
    canvas,
    now,
    total,
    play,
    loop,
    selectionBar: h("div", { class: "selection-bar" }),
    zoom: h("div", { class: "zoom", role: "group", "aria-label": "파형 확대" }),
    overview: h("canvas", { class: "wave-overview", "aria-label": "곡 전체. 끌어서 확대한 부분을 옮겨요." }),
    compare: h("div", { class: "compare" }),
    versions: h("section", { class: "card versions-card", "aria-label": "버전" }),
    tabs: h("div", { class: "tabs", role: "tablist" }),
    info: h("div", { class: "info-panel", role: "tabpanel" }),
    fixTarget: h("p", { class: "fix-target" }),
    fixScope: h("div", { class: "fix-scope" }),
    fixChips: h("div", { class: "fix-chips" }),
    fixInstruments: h("details", { class: "disclosure fix-instruments" }),
    fixArea,
    fixOptions: h("div", { class: "fix-options" }),
    planButton,
    assistantLabel: h("button", { type: "button", class: "assistant-label", onClick: () => go("settings") }),
    planBox: h("div", { class: "plan-box" }),
    history: h("div", { class: "history" }),
    noteArea,
    lyricsArea,
  };
  els = refs;

  mount(
    root,
    h(
      "div",
      { class: "studio" },
      refs.head,
      refs.banner,
      h(
        "div",
        { class: "studio-grid" },
        h(
          "div",
          { class: "studio-main" },
          h(
            "section",
            { class: "card player-card", "aria-label": "재생" },
            h(
              "div",
              { class: "player-top" },
              play,
              refs.nowPlaying,
              h(
                "div",
                { class: "transport" },
                h("button", { type: "button", class: "transport-icon", "aria-label": "처음으로", "data-tip": "처음으로", onClick: () => player?.seek(get().selection?.startSeconds ?? 0) }, icon("start", 17)),
                h("button", { type: "button", class: "transport-icon", "aria-label": "5초 뒤로", "data-tip": "5초 뒤로 (←)", onClick: () => nudge(-5) }, icon("back5", 19)),
                h("button", { type: "button", class: "transport-icon", "aria-label": "5초 앞으로", "data-tip": "5초 앞으로 (→)", onClick: () => nudge(5) }, icon("fwd5", 19)),
                h("span", { class: "clock" }, now, h("span", { class: "clock-sep" }, "/"), total),
                h("label", { class: "volume-wrap", "data-tip": "음량" }, icon("volume", 16), volume),
              ),
            ),
            h("div", { class: "wave-wrap" }, canvas),
            refs.overview,
            h("div", { class: "player-foot" }, refs.selectionBar, refs.zoom),
            refs.compare,
          ),
          refs.versions,
          h("section", { class: "card info-card", "aria-label": "이 버전 정보" }, refs.tabs, refs.info),
        ),
        h(
          "aside",
          { class: "fix-panel", "aria-label": "고치기" },
          h("div", { class: "fix-head" }, h("h2", null, icon("wand", 17), "고치기"), refs.fixTarget),
          refs.fixScope,
          fixArea,
          refs.fixChips,
          refs.fixInstruments,
          refs.fixOptions,
          planButton,
          refs.assistantLabel,
          refs.planBox,
          refs.history,
        ),
      ),
    ),
  );

  player = new Player(canvas);
  player.audio.volume = Number(localStorageGet("volume", "0.9"));
  player.onTick = () => {
    if (!player || !els) return;
    els.now.textContent = clock(player.time, true);
    els.total.textContent = clock(player.length, true);
    const playing = player.playing;
    if (els.play.dataset.state !== (playing ? "pause" : "play")) {
      els.play.dataset.state = playing ? "pause" : "play";
      els.play.replaceChildren(icon(playing ? "pause" : "play", 24));
      els.play.setAttribute("aria-label", playing ? "멈춤" : "재생");
    }
  };
  player.onSelection = (range) => {
    set({ selection: range, scope: range ? "range" : "song", ...(range ? {} : { loop: false }) });
  };
  player.attachOverview(refs.overview);
  player.onView = () => renderZoom();
  renderZoom();
}

function renderZoom(): void {
  if (!els || !player) return;
  const zoom = player.zoom;
  els.overview.hidden = zoom <= 1;
  const label = zoom <= 1 ? "전체" : `${zoom >= 10 ? Math.round(zoom) : Math.round(zoom * 10) / 10}×`;
  mount(
    els.zoom,
    h("button", { type: "button", class: "icon-button small", "aria-label": "축소", "data-tip": "축소 (−)", disabled: zoom <= 1, onClick: () => zoomWave(-1) }, h("span", { class: "zoom-glyph" }, "−")),
    h("button", { type: "button", class: "zoom-level", "data-tip": "⌘ + 스크롤이나 두 손가락 벌리기로 확대, 옆으로 밀어 이동해요. 누르면 전체 보기 (0)", onClick: () => zoomWave(0) }, label),
    h("button", { type: "button", class: "icon-button small", "aria-label": "확대", "data-tip": "확대 (+)", onClick: () => zoomWave(1) }, h("span", { class: "zoom-glyph" }, "+")),
    h("button", { type: "button", class: "button ghost small", disabled: !get().selection, "data-tip": "고른 구간을 크게 봐요", onClick: () => player?.fitSelection() }, "구간 맞춤"),
  );
}

function localStorageGet(key: string, fallback: string): string {
  try {
    return localStorage.getItem(`music-studio:${key}`) ?? fallback;
  } catch {
    return fallback;
  }
}

function localStorageSet(key: string, value: string): void {
  try {
    localStorage.setItem(`music-studio:${key}`, value);
  } catch {
    // per-viewer convenience only
  }
}

// ---------------------------------------------------------------------------
// Head and banners
// ---------------------------------------------------------------------------

function renderHead(song: Song): void {
  if (!els) return;
  const t = tree();
  const final = node(song.finalVersionId);
  const recommended = node(song.recommendedVersionId);
  const exporting = final ?? recommended;
  const fulls = t?.roots.filter((item) => !isAutomaticAttempt(item.version)).length ?? 0;
  const edits = song.versions.filter((item) => !isAutomaticAttempt(item) && item.kind === "edit").length;
  const busy = songBusy();
  const current = activeVersion();
  const currentNode = node(current?.id);
  mount(
    els.head,
    h(
      "div",
      { class: "studio-title" },
      h("h1", null, song.title),
      h(
        "p",
        null,
        [
          lengthLabel(song.inputs.durationSeconds),
          `버전 ${fulls}개${edits ? ` · 수정 ${edits}개` : ""}`,
          final ? `최종본 ${final.short}` : recommended ? `추천 ${recommended.short}` : null,
          relativeTime(song.updatedAt),
        ]
          .filter(Boolean)
          .join(" · "),
      ),
    ),
    h(
      "div",
      { class: "head-actions" },
      menu({ label: "곡 메뉴", icon: "more", className: "icon-button bordered", tip: "곡 메뉴" }, [
        { label: "곡 설정 고치기", icon: "edit", disabled: busy, tip: "제목·스타일·가사·길이. 다음 버전부터 적용돼요", run: () => openSongSettings(song) },
        current && currentNode && { label: `${currentNode.short}의 가사·편곡으로 새로 만들기`, icon: "refresh", disabled: busy || !current.fileOk, run: () => openRegenerateSong(song, current, currentNode.name) },
        song.canUndoFinal && { label: "최종본 지정 되돌리기", icon: "undo", disabled: busy, run: () => void undoFinal(song) },
        { label: "Finder에서 곡 폴더 보기", icon: "folder", run: () => reveal({ kind: "song", songId: song.songId }) },
      ]),
      h(
        "button",
        {
          type: "button",
          class: "button primary",
          disabled: !exporting || busy,
          "data-tip": exporting ? `${exporting.name}${final ? " (최종본)" : " (자동 추천)"}을 WAV로 저장해요` : "버전이 준비되면 WAV로 저장할 수 있어요",
          onClick: (event: Event) => void exportFinal(event.currentTarget as HTMLButtonElement, song.songId),
        },
        icon("export", 16),
        "WAV 내보내기",
      ),
    ),
  );
}

function renderBanner(state: State, song: Song): void {
  if (!els) return;
  const task = state.task && state.task.songId === song.songId ? state.task : null;
  if (task) {
    mount(
      els.banner,
      h(
        "div",
        { class: "task-banner" },
        h("span", { class: "task-spinner", "aria-hidden": "true" }),
        h("div", { class: "task-text" }, h("b", null, task.label), h("span", null, task.stage, task.detail ? ` · ${task.detail}` : "")),
        h("div", { class: "meter" }, h("i", { style: { width: `${Math.round(task.progress * 100)}%` } })),
        h("span", { class: "task-figures mono" }, task.total > 1 ? `${task.done}/${task.total} · ` : "", elapsed(task.startedAt)),
        h(
          "button",
          {
            type: "button",
            class: "button ghost small",
            disabled: task.cancelling,
            onClick: async () => {
              const ok = await confirmDialog({
                title: "만들기를 취소할까요?",
                body: "이미 끝난 버전은 남아 있어요. 엔진에 보낸 한 곡은 취소 후에도 끝까지 계산될 수 있어요.",
                confirm: "취소하기",
                danger: true,
              });
              if (!ok) return;
              try {
                await api.cancelTask(task.songId, task.startedAt);
              } catch (error) {
                toast(errorText(error), { tone: "error" });
              }
            },
          },
          task.cancelling ? "취소하는 중" : "취소",
        ),
      ),
    );
    return;
  }
  if (song.generationActive) {
    mount(els.banner, h("div", { class: "notice row", role: "status" },
      h("p", null, h("b", null, "다른 창이나 터미널에서 만드는 중이에요. "), "평가와 메모는 지금 저장할 수 있어요."),
      h("button", { type: "button", class: "button small ghost", onClick: () => void refreshSong(song.songId) }, "새로 고침"),
    ));
    return;
  }
  const resumable = [...song.jobs].reverse().filter((job) => job.canResume && job.kind === "candidate-batch");
  if (resumable.length) {
    mount(els.banner, ...resumable.map((job) => {
      const done = (job.succeeded ?? 0) + (job.reused ?? 0);
      const total = job.seeds?.length ?? 0;
      const label = job.status === "cancelled" ? "취소했던 만들기" : job.status === "partial" ? "일부 버전을 못 만든 작업" : job.status === "failed" ? "완료하지 못한 만들기" : "중단된 만들기";
      return h("div", { class: "notice tone-warn row", role: "status" },
        h("div", null,
          h("p", null, h("b", null, `${label} · ${done}/${total}개 완료`), " 처음 요청 그대로 남은 버전을 이어 만들어요."),
          job.error || job.failures?.length ? h("p", { class: "footnote" }, (job.error ?? job.failures?.[0]?.error ?? "").slice(0, 180)) : null,
        ),
        h("button", {
          type: "button", class: "button small secondary", disabled: !engineReady() || Boolean(state.task),
          "data-tip": !engineReady() ? "음악 엔진을 먼저 켜세요" : state.task ? "진행 중인 작업이 끝나면 이어 만들 수 있어요" : "",
          onClick: (event: Event) => void withBusy(event.currentTarget as HTMLButtonElement, "이어서 만드는 중",
            async () => {
              const ticket = songTicket(song.songId);
              if (ticket) applySongForContext(await api.resume(song.songId, job.jobId), ticket);
            }),
        }, "이어서 만들기"),
      );
    }));
    return;
  }
  const interrupted = song.jobs.some((job) => job.status === "interrupted" && !job.resumedByJobId);
  if (interrupted) {
    mount(els.banner, h("div", { class: "notice tone-warn row" },
      h("p", null, "끝나지 않은 작업이 있어요. 완료한 버전은 남아 있어요."),
    ));
    return;
  }
  els.banner.replaceChildren();
}

// ---------------------------------------------------------------------------
// Player
// ---------------------------------------------------------------------------

function renderPlayer(state: State, song: Song): void {
  if (!els || !player) return;
  const current = node(state.activeVersionId);
  if (!current) {
    const making = state.task && state.task.songId === song.songId;
    mount(els.nowPlaying, h("b", { class: "np-title" }, making ? "첫 버전을 만드는 중이에요" : "아직 버전이 없어요"),
      h("span", { class: "np-sub" }, making ? "끝나는 대로 여기서 바로 들을 수 있어요" : "아래 ‘더 만들기’로 버전을 만들어 보세요"));
    player.unavailable = making ? "버전이 끝나는 대로 여기서 들을 수 있어요" : "아직 들을 버전이 없어요";
    void player.load(null);
    els.selectionBar.replaceChildren();
    els.compare.replaceChildren();
    return;
  }
  const version = current.version;
  const parent = node(versionSourceParent(song, version)?.id);
  const compare = node(comparisonVersion(song, version, state.comparisonVersionId)?.id);
  const listeningComparison = Boolean(state.listenToParent && compare);
  const quality = qualityLabel(version.quality);
  mount(
    els.nowPlaying,
    h("b", { class: "np-title" }, listeningComparison ? compare!.name : current.name),
    h(
      "span",
      { class: "np-sub" },
      listeningComparison
        ? h("span", { class: "pill tone-edit" }, "비교 중 · C로 돌아가기")
        : [
            version.isFinal && h("span", { class: "pill tone-accent" }, "최종본"),
            version.recommended && !version.isFinal && h("span", { class: "pill tone-accent" }, "자동 추천"),
            quality && h("span", { class: `pill tone-${quality.tone}` }, quality.label),
            version.editRange && parent && h("span", { class: "pill tone-edit" }, `${parent.short}의 ${rangeLabel(version.editRange)} 수정`),
            version.kind === "cover" && parent && h("span", { class: "pill tone-edit" }, `${parent.short} 바탕`),
            h("span", { class: `pill tone-${version.review.status}` }, reviewCopy[version.review.status].label),
          ],
    ),
  );
  const source = listeningComparison ? compare!.version : version;
  player.editRange = version.editRange;
  player.overlay = listeningComparison ? `비교 중: ${compare!.name}` : null;
  player.unavailable = source.fileOk ? null : "이 버전의 음원 파일을 쓸 수 없어요";
  player.selection = state.selection;
  player.loop = state.loop;
  void player.load({ key: source.id, url: source.audioUrl, peaks: source.waveform, duration: source.durationSeconds });
  renderSelectionBar(state);
  renderCompare(state, song, version, current);
}

function renderCompare(state: State, song: Song, version: Version, current: VersionNode): void {
  if (!els) return;
  const choices = comparisonChoices(song, version);
  if (!choices.length) {
    els.compare.replaceChildren();
    return;
  }
  const parent = node(versionSourceParent(song, version)?.id);
  const compare = node(comparisonVersion(song, version, state.comparisonVersionId)?.id);
  const listening = Boolean(state.listenToParent && compare);
  mount(
    els.compare,
    h("span", { class: "sublabel" }, "비교할 버전"),
    h(
      "select",
      {
        class: "input small",
        "aria-label": "비교할 버전",
        onChange: (event: Event) => set({ comparisonVersionId: (event.target as HTMLSelectElement).value || null, listenToParent: false }),
      },
      h("option", { value: "", selected: !state.comparisonVersionId || !compare }, parent ? `원본 · ${parent.short}` : "버전 고르기"),
      choices.map((item) => h("option", { value: item.id, selected: state.comparisonVersionId === item.id }, node(item.id)?.name ?? "버전")),
    ),
    compare &&
      h(
        "div",
        { class: "segmented small", role: "group", "aria-label": "두 버전 번갈아 듣기" },
        h("button", { type: "button", class: listening ? "" : "is-active", "aria-pressed": listening ? "false" : "true", "data-tip": `${current.name} (C로 전환)`, onClick: () => set({ listenToParent: false }) }, "이 버전"),
        h("button", { type: "button", class: listening ? "is-active" : "", "aria-pressed": listening ? "true" : "false", "data-tip": `${compare.name} · 같은 위치에서 들어요 (C)`, onClick: () => set({ listenToParent: true }) }, "비교 버전"),
      ),
  );
}

// "1:02.35", "62.35" or "0:29" → seconds.
function parseClock(text: string): number | null {
  const match = text.trim().match(/^(?:(\d+):)?(\d+(?:\.\d+)?)$/);
  if (!match) return null;
  const seconds = Number(match[1] ?? 0) * 60 + Number(match[2]);
  return Number.isFinite(seconds) ? seconds : null;
}

// Moves one edge of the selection, keeping at least 0.3 s and staying inside the take.
function moveEdge(edge: "start" | "end", seconds: number): void {
  const range = get().selection;
  const length = player?.length ?? 0;
  if (!range || !length) return;
  const value = Math.round(seconds * 100) / 100;
  const next = edge === "start"
    ? { startSeconds: Math.max(0, Math.min(value, range.endSeconds - 0.3)), endSeconds: range.endSeconds }
    : { startSeconds: range.startSeconds, endSeconds: Math.min(length, Math.max(value, range.startSeconds + 0.3)) };
  set({ selection: next, scope: "range" });
}

function edgeField(edge: "start" | "end", label: string, seconds: number): HTMLElement {
  const input = h("input", {
    class: "input small time-input mono",
    value: clock(seconds, true, 2),
    "aria-label": `${label} 시간`,
    onKeydown: (event: KeyboardEvent) => {
      if (event.key === "Enter") (event.target as HTMLInputElement).blur();
      if (event.key === "Escape") {
        (event.target as HTMLInputElement).value = clock(seconds, true, 2);
        (event.target as HTMLInputElement).blur();
      }
      event.stopPropagation();
    },
    onChange: (event: Event) => {
      const parsed = parseClock((event.target as HTMLInputElement).value);
      if (parsed === null) {
        toast("시간은 1:02.35처럼 적어 주세요.", { tone: "error" });
        (event.target as HTMLInputElement).value = clock(seconds, true, 2);
        return;
      }
      moveEdge(edge, parsed);
    },
  });
  const nudgeEdge = (delta: number) => (event: MouseEvent) => moveEdge(edge, seconds + delta * (event.shiftKey ? 10 : 1));
  return h("span", { class: "edge-field" },
    h("span", { class: "sublabel" }, label),
    h("button", { type: "button", class: "icon-button small", "aria-label": `${label} 0.1초 앞으로`, "data-tip": "0.1초 당기기 (Shift: 1초)", onClick: nudgeEdge(-0.1) }, icon("back", 14)),
    input,
    h("button", { type: "button", class: "icon-button small", "aria-label": `${label} 0.1초 뒤로`, "data-tip": "0.1초 밀기 (Shift: 1초)", onClick: nudgeEdge(0.1) }, icon("forward", 14)),
  );
}

function renderSelectionBar(state: State): void {
  if (!els) return;
  const range = state.selection;
  els.loop.classList.toggle("is-active", state.loop && Boolean(range));
  els.loop.setAttribute("aria-pressed", state.loop && range ? "true" : "false");
  renderZoom();
  if (!range) {
    mount(els.selectionBar, h("span", { class: "hint" }, "파형을 끌면 구간을 골라요 · ⌘ + 스크롤로 확대해 더 정확하게 잡을 수 있어요"));
    return;
  }
  mount(
    els.selectionBar,
    edgeField("start", "시작", range.startSeconds),
    edgeField("end", "끝", range.endSeconds),
    h("span", { class: "muted selection-length" }, `${(range.endSeconds - range.startSeconds).toFixed(2)}초`),
    els.loop,
    h("button", { type: "button", class: "button secondary small", onClick: () => { set({ scope: "range" }); focusFeedback(); } }, icon("wand", 14), "이 구간 고치기"),
    h("button", { type: "button", class: "icon-button small", "aria-label": "선택 해제", "data-tip": "선택 해제 (Esc)", onClick: () => clearSelection() }, icon("close", 14)),
  );
}

// ---------------------------------------------------------------------------
// Versions
// ---------------------------------------------------------------------------

async function saveReview(songId: string, version: Version, status: ReviewStatus, rating: number | null, note?: string): Promise<boolean> {
  try {
    const ticket = songTicket(songId);
    if (!ticket) return false;
    return applySongForContext(await api.review({ songId, versionId: version.id, status, rating, note }), ticket);
  } catch (error) {
    toast(errorText(error), { tone: "error" });
    return false;
  }
}

async function setFinal(song: Song, version: Version, name: string): Promise<void> {
  try {
    const ticket = songTicket(song.songId);
    if (!ticket || !applySongForContext(await api.setFinal(song.songId, version.id), ticket)) return;
    toast(`${name}을 최종본으로 정했어요. WAV 내보내기는 이 버전을 저장해요.`, { tone: "ok" });
  } catch (error) {
    toast(errorText(error), { tone: "error" });
  }
}

async function undoFinal(song: Song): Promise<void> {
  try {
    const ticket = songTicket(song.songId);
    if (!ticket || !applySongForContext(await api.undoFinal(song.songId), ticket)) return;
    toast("최종본 지정을 되돌렸어요.");
  } catch (error) {
    toast(errorText(error), { tone: "error" });
  }
}

function versionRow(song: Song, item: VersionNode, state: State, busy: boolean): HTMLElement {
  const version = item.version;
  const active = version.id === state.activeVersionId;
  const automatic = isAutomaticAttempt(version);
  const quality = qualityLabel(version.quality);
  const review = version.review.status;
  const thumb = (status: ReviewStatus, iconName: string, label: string) =>
    h(
      "button",
      {
        type: "button",
        class: `row-icon tone-${status}${review === status ? " is-on" : ""}`,
        "aria-pressed": review === status ? "true" : "false",
        "aria-label": label,
        "data-tip": review === status ? `${label} 취소` : label,
        onClick: () => void saveReview(song.songId, version, review === status ? "listened" : status, version.review.rating),
      },
      icon(iconName, 15),
    );
  return h(
    "div",
    { class: `version-row depth-${Math.min(item.depth, 3)}${active ? " is-active" : ""}${version.fileOk ? "" : " is-broken"}`, "aria-current": active ? "true" : undefined },
    h(
      "button",
      { type: "button", class: "version-play", "aria-label": `${item.name} 듣기`, "data-tip": "듣기", disabled: !version.fileOk, onClick: () => selectVersion(version.id, true) },
      h("span", { class: "version-index" }, automatic ? String(version.quality?.attempt ?? "·") : item.depth ? icon("branch", 14) : item.short.replace("버전 ", "")),
      h("span", { class: "version-hover" }, icon("play", 14)),
    ),
    h(
      "button",
      { type: "button", class: "version-main", onClick: () => selectVersion(version.id) },
      h("b", null, item.short),
      h("small", null,
        version.fileOk ? (version.editRange ? `${rangeLabel(version.editRange)} 수정` : lengthLabel(version.durationSeconds)) : "파일 없음",
        quality ? ` · ${quality.label}` : "",
      ),
    ),
    h(
      "div",
      { class: "version-tags" },
      version.isFinal ? h("span", { class: "pill tone-accent" }, "최종본") : version.recommended ? h("span", { class: "pill tone-accent" }, "추천") : null,
      review === "unreviewed" && !automatic && h("span", { class: "new-dot", "data-tip": "아직 안 들음" }),
    ),
    !automatic &&
      h(
        "div",
        { class: "version-actions" },
        thumb("approved", "thumbUp", "좋아요"),
        thumb("rejected", "thumbDown", "별로예요"),
        h(
          "button",
          {
            type: "button",
            class: `row-icon tone-final${version.isFinal ? " is-on" : ""}`,
            disabled: busy || !version.fileOk || version.isFinal,
            "aria-label": "최종본으로 정하기",
            "data-tip": version.isFinal ? "이 버전이 최종본이에요" : "최종본으로 정하기 — WAV 내보내기에 쓰여요",
            onClick: () => void setFinal(song, version, item.name),
          },
          icon("flag", 15),
        ),
      ),
  );
}

function renderVersions(state: State, song: Song): void {
  if (!els) return;
  const t = tree();
  if (!t) return;
  const task = state.task && state.task.songId === song.songId ? state.task : null;
  const busy = songBusy();
  const rows: Child[] = [];
  const attempts: Child[] = [];
  const placeholder = (label: string, depth: number) =>
    h("div", { class: `version-row is-pending depth-${depth}`, "aria-hidden": "true" },
      h("span", { class: "version-play" }, h("span", { class: "task-spinner small" })),
      h("span", { class: "version-main" }, h("b", null, label), h("small", null, "만드는 중")));
  for (const item of t.flat) {
    const row = versionRow(song, item, state, busy);
    (isAutomaticAttempt(item.version) ? attempts : rows).push(row);
    if (task?.kind === "repaint" && song.feedback.at(-1)?.candidateId === item.version.id && !song.versions.some((version) => version.feedbackId === song.feedback.at(-1)?.feedbackId)) {
      rows.push(placeholder(`수정 ${item.children.length + 1}`, Math.min(item.depth + 1, 3)));
    }
  }
  if (task && task.kind !== "repaint") {
    const remaining = Math.max(0, task.total - task.done);
    const ready = t.roots.filter((item) => !isAutomaticAttempt(item.version)).length;
    for (let index = 0; index < remaining; index += 1) rows.push(placeholder(`버전 ${ready + index + 1}`, 0));
  }
  const ready = engineReady();
  const defaults = state.settings.defaultVersions;
  mount(
    els.versions,
    h(
      "header",
      { class: "card-head" },
      h("h2", null, "버전 ", h("span", { class: "count" }, String(song.versions.filter((version) => !isAutomaticAttempt(version)).length))),
      menu(
        { label: "같은 설정으로 더 만들기", icon: "plus", className: `button ghost small${busy || !ready ? " is-disabled" : ""}`, tip: ready ? "지금 곡 설정 그대로 새 버전을 더 만들어요" : "음악 엔진을 켜야 만들 수 있어요" },
        [1, 2, 4].map((count) => ({
          label: `${count}개 더 만들기${count === defaults ? " (기본)" : ""}`,
          disabled: busy || !ready,
          run: async () => {
            try {
              const ticket = songTicket(song.songId);
              if (!ticket || !applySongForContext(await api.generateMore(song.songId, count), ticket)) return;
              toast(`버전 ${count}개를 더 만들기 시작했어요.`, { tone: "ok" });
            } catch (error) {
              toast(errorText(error), { tone: "error" });
            }
          },
        })),
      ),
    ),
    rows.length ? h("div", { class: "version-list" }, rows) : h("p", { class: "empty-line" }, "아직 버전이 없어요. ‘더 만들기’로 시작하세요."),
    attempts.length
      ? h("details", { class: "disclosure attempts", open: Boolean(activeVersion() && isAutomaticAttempt(activeVersion()!)) },
          h("summary", null, `자동 재시도 기록 ${attempts.length}개 `, h("em", null, "처음 만든 원본과 다시 만든 시도")),
          h("div", { class: "version-list" }, attempts))
      : null,
  );
}

// ---------------------------------------------------------------------------
// Info card: prompt · lyrics · checks · notes
// ---------------------------------------------------------------------------

function previousFull(song: Song, version: Version): Version | null {
  const fulls = song.versions.filter((item) => !isAutomaticAttempt(item) && item.kind === "full");
  const index = fulls.findIndex((item) => item.id === version.id);
  return index > 0 ? fulls[index - 1] : null;
}

function renderTabs(state: State): void {
  if (!els) return;
  const version = activeVersion();
  els.tabs.hidden = !version;
  const warnings = version?.findings.filter((item) => item.severity !== "info").length ?? 0;
  const notes = version?.review.notes.length ?? 0;
  const tabs: Array<[InspectorTab, string, Child]> = [
    ["prompt", "만든 프롬프트", null],
    ["lyrics", "가사", null],
    ["checks", "자동 검사", warnings ? h("em", { class: "tab-count" }, String(warnings)) : null],
    ["notes", "평가·메모", notes ? h("em", { class: "tab-count muted" }, String(notes)) : null],
  ];
  mount(
    els.tabs,
    tabs.map(([key, label, extra]) =>
      h("button", { type: "button", role: "tab", class: state.tab === key ? "is-active" : "", "aria-selected": state.tab === key ? "true" : "false", onClick: () => set({ tab: key }) }, label, extra),
    ),
  );
}

function renderInfo(state: State, song: Song): void {
  if (!els) return;
  const version = activeVersion();
  if (!version) {
    mount(els.info, h("p", { class: "empty-line" }, "버전을 고르면 만든 프롬프트와 가사, 검사 결과를 볼 수 있어요."));
    return;
  }
  if (state.tab === "lyrics") mount(els.info, lyricsPanel(state, song, version));
  else if (state.tab === "checks") mount(els.info, checksPanel(song, version));
  else if (state.tab === "notes") mount(els.info, notesPanel(song, version));
  else mount(els.info, promptPanel(song, version));
}

function promptPanel(song: Song, version: Version): Child {
  const parent = node(versionSourceParent(song, version)?.id);
  const baselineVersion = parent?.version ?? previousFull(song, version);
  const baselineName = parent?.short ?? (baselineVersion ? node(baselineVersion.id)?.short : null);
  const diff = baselineVersion ? tagDiff(baselineVersion.stylePrompt, version.stylePrompt) : [];
  const feedback = song.feedback.find((item) => item.feedbackId === version.feedbackId);
  const strength = strengthFromValue(version.repaintStrength);
  const production = version.productionRules;
  return [
    feedback &&
      h("div", { class: "request" },
        h("span", { class: "sublabel" }, "내 요청"),
        h("p", { class: "request-text" }, `“${feedback.text || "요청 없이 다시 만들기"}”`),
        feedback.plan && h("p", { class: "hint" }, feedback.plan.summary),
      ),
    baselineVersion && diff.length > 0 &&
      h("div", { class: "diff" },
        h("span", { class: "sublabel" }, `${baselineName}에서 바뀐 태그`),
        h("div", { class: "chips" }, diff.map((change) => h("span", { class: `tag-chip op-${change.op}` }, change.op === "add" ? "+ " : "− ", change.term))),
      ),
    h("div", { class: "prompt-block" },
      h("div", { class: "field-head" },
        h("span", { class: "sublabel" }, "스타일 프롬프트"),
        h("button", { type: "button", class: "button ghost small", onClick: async () => { await navigator.clipboard.writeText(version.stylePrompt); toast("스타일 프롬프트를 복사했어요."); } }, icon("copy", 14), "복사"),
      ),
      h("p", { class: "caption-text mono" }, version.stylePrompt),
      baselineVersion && !diff.length && h("p", { class: "hint" }, version.editRange ? `${baselineName}과 같은 스타일로 구간만 다시 만들었어요.` : `${baselineName}과 스타일이 같아요.`),
    ),
    h(
      "dl",
      { class: "facts inline" },
      h("dt", null, "시드"), h("dd", { class: "mono" }, String(version.seed ?? "—")),
      h("dt", null, "모델"), h("dd", { class: "mono" }, version.model ?? "—"),
      version.bpm ? [h("dt", null, "빠르기"), h("dd", { class: "mono" }, `${version.bpm} BPM`)] : null,
      version.keyScale ? [h("dt", null, "조성"), h("dd", { class: "mono" }, version.keyScale)] : null,
      strength ? [h("dt", null, "변화"), h("dd", null, strengthCopy[strength].label)] : null,
      h("dt", null, "만든 때"), h("dd", null, relativeTime(version.createdAt)),
    ),
    production && (production.preset || production.rules.length > 0) &&
      h("details", { class: "disclosure" },
        h("summary", null, `함께 보낸 제작 규칙${production.preset ? ` · ${production.preset.label}` : ""}`),
        h("ul", { class: "plain-list" }, production.rules.filter((rule) => production.appliedRuleIds.includes(rule.id)).map((rule) => h("li", null, rule.label))),
        production.skippedRuleIds.length > 0 && h("p", { class: "hint" }, "연주곡이라 보컬·가사 호흡 규칙은 보내지 않았어요."),
      ),
    version.songPlan && h("details", { class: "disclosure" }, h("summary", null, "함께 보낸 전개·호흡 안내"), songPlanView(version.songPlan)),
    version.instruction &&
      h("div", { class: "notice tone-warn" },
        h("p", null, h("b", null, "예전 방식으로 만든 버전이에요. "), "자유 문장이 엔진의 작업 지시 자리를 덮어써서 요청이 제대로 반영되지 않았을 수 있어요."),
        h("p", { class: "mono legacy-instruction" }, version.instruction),
      ),
    version.coverSource && h("p", { class: "hint" }, `원본 음원을 참조해 만든 커버예요 · 원본 따르기 ${Math.round((version.coverStrength ?? 0.7) * 100)}%`),
  ];
}

function editableLyrics(version: Version): string {
  // The words as written, before section guidance was added; repaint and regenerate
  // re-apply that guidance themselves.
  return version.lyricsOriginal ?? version.lyrics;
}

function lyricsChanged(): boolean {
  return Boolean(els && lyricsEdit && els.lyricsArea.value.trim() && els.lyricsArea.value !== lyricsEdit.base);
}

function syncLyricsActions(): void {
  const changed = lyricsChanged();
  for (const button of lyricsActions) {
    if (button.classList.contains("is-busy")) continue;
    const reason = songBusy() ? "지금 작업이 끝난 뒤 만들 수 있어요"
      : !engineReady() ? "음악 엔진을 켜야 만들 수 있어요"
        : button.dataset.needsRange === "true" ? "파형에서 바꾼 줄이 불리는 부분을 먼저 끌어서 고르세요"
          : !changed ? "가사를 바꾸면 만들 수 있어요" : "";
    button.disabled = Boolean(reason);
    button.dataset.tip = reason;
  }
}

function startLyricsEdit(version: Version): void {
  if (!els) return;
  const base = editableLyrics(version);
  lyricsEdit = { versionId: version.id, base };
  els.lyricsArea.value = base;
  const state = get();
  const song = state.song.song;
  if (song) renderInfo(state, song);
  els.lyricsArea.focus();
  els.lyricsArea.setSelectionRange(0, 0);
  els.lyricsArea.scrollTop = 0;
}

function stopLyricsEdit(): void {
  lyricsEdit = null;
  const state = get();
  if (state.song.song) renderInfo(state, state.song.song);
}

async function repaintWithLyrics(button: HTMLButtonElement, song: Song, version: Version): Promise<void> {
  const range = get().selection;
  if (!els || !range || !lyricsChanged()) return;
  const ticket = songTicket(song.songId);
  if (!ticket) return;
  const lyrics = els.lyricsArea.value.trim();
  const strength = get().strength;
  // The same request the fix panel sends, with only the words changed: same version,
  // same style, the selected range repainted.
  const plan: Plan = {
    action: "repaint",
    summary: `${rangeLabel(range)} 구간을 고친 가사로 다시 만들어요.`,
    stylePrompt: version.baseStylePrompt || version.stylePrompt,
    baseStylePrompt: version.baseStylePrompt || version.stylePrompt,
    lyrics,
    bpm: null,
    range: { ...range },
    strength,
    versions: 1,
    changes: [],
    assistant: "rules",
    assistantModel: null,
    understood: true,
    notes: [],
    candidateId: version.id,
  };
  await withBusy(button, "시작하는 중", async () => {
    const next = await api.applyPlan(song.songId, plan, `가사 고치기 · ${rangeLabel(range)}`);
    if (!applySongForContext(next, ticket)) return;
    lyricsEdit = null;
    toast(`${rangeLabel(range)} 구간을 고친 가사로 다시 만들기 시작했어요. 끝나면 새 수정본이 열려요.`, { tone: "ok" });
  });
  if (get().song.song) renderInfo(get(), get().song.song!);
}

async function regenerateWithLyrics(button: HTMLButtonElement, song: Song, version: Version): Promise<void> {
  if (!els || !lyricsChanged()) return;
  const ticket = songTicket(song.songId);
  if (!ticket) return;
  const lyrics = els.lyricsArea.value.trim();
  await withBusy(button, "시작하는 중", async () => {
    const next = await api.regenerateSong({ songId: song.songId, versionId: version.id, versions: 1, lyrics });
    if (!applySongForContext(next, ticket)) return;
    lyricsEdit = null;
    toast("고친 가사로 새 전체 버전을 만들기 시작했어요.", { tone: "ok" });
  });
  if (get().song.song) renderInfo(get(), get().song.song!);
}

function lyricsPanel(state: State, song: Song, version: Version): Child {
  if (!els) return null;
  if (lyricsEdit?.versionId !== version.id) {
    lyricsActions = [];
    return [
      h("div", { class: "field-head" },
        h("span", { class: "sublabel" }, "이 버전에 보낸 가사"),
        h("button", {
          type: "button",
          class: "button secondary small",
          disabled: !version.fileOk || version.lyrics.trim() === "[Instrumental]",
          "data-tip": version.lyrics.trim() === "[Instrumental]" ? "연주곡이라 가사가 없어요" : "가사를 고쳐 그 구간만, 또는 곡 전체를 다시 만들어요",
          onClick: () => startLyricsEdit(version),
        }, icon("edit", 14), "가사 고치기"),
      ),
      h("pre", { class: "lyrics-view tall" }, version.lyrics),
      version.lyricsOriginal !== undefined && version.lyricsOriginal !== version.lyrics &&
        h("details", { class: "disclosure" }, h("summary", null, "제작 안내를 넣기 전 원문"), h("pre", { class: "lyrics-view" }, version.lyricsOriginal)),
    ];
  }
  const range = state.selection;
  const rangeButton = h("button", {
    type: "button",
    class: "button primary",
    dataset: { needsRange: range ? "false" : "true" },
    onClick: (event: Event) => void repaintWithLyrics(event.currentTarget as HTMLButtonElement, song, version),
  }, icon("wand", 15), range ? `고친 가사로 ${rangeLabel(range)}만 다시 만들기` : "고친 가사로 구간만 다시 만들기");
  const wholeButton = h("button", {
    type: "button",
    class: "button secondary",
    onClick: (event: Event) => void regenerateWithLyrics(event.currentTarget as HTMLButtonElement, song, version),
  }, icon("refresh", 15), "고친 가사로 새 전체 버전");
  lyricsActions = [rangeButton, wholeButton];
  const panel: Child = [
    h("div", { class: "field-head" },
      h("span", { class: "sublabel" }, `${node(version.id)?.short ?? "이 버전"}의 가사 고치기`),
      h("button", { type: "button", class: "button ghost small", onClick: () => { if (els && lyricsEdit) els.lyricsArea.value = lyricsEdit.base; syncLyricsActions(); } }, icon("undo", 14), "처음 가사로"),
    ),
    els.lyricsArea,
    h("div", { class: "lyrics-edit-options" },
      range
        ? h("div", { class: "lyrics-edit-range" },
            h("span", { class: "hint" }, `선택한 ${rangeLabel(range)}만 다시 만들고 나머지는 그대로 둬요. 얼마나 바꿀까요?`),
            h("div", { class: "segmented small" }, (["light", "medium", "strong"] as Strength[]).map((key) =>
              h("button", { type: "button", class: state.strength === key ? "is-active" : "", "aria-pressed": state.strength === key ? "true" : "false", "data-tip": strengthCopy[key].hint, onClick: () => set({ strength: key }) }, strengthCopy[key].label))),
          )
        : h("p", { class: "hint" }, "바꾼 줄이 불리는 부분을 위 파형에서 끌어 고르면 그 구간만 다시 만들 수 있어요. 고르지 않으면 곡 전체를 새로 만들어요."),
    ),
    h("div", { class: "plan-buttons" }, rangeButton, wholeButton, h("button", { type: "button", class: "button ghost", onClick: () => stopLyricsEdit() }, "취소")),
    h("p", { class: "footnote" }, "원래 버전은 그대로 남아요. 새 결과는 버전 목록에 수정본으로 쌓이고, 고친 기록에도 남아요."),
  ];
  // Buttons enable only once the words differ, the engine is on and nothing else runs.
  queueMicrotask(syncLyricsActions);
  return panel;
}

function qualityReport(version: Version): Child {
  const quality = version.quality;
  if (!quality) return null;
  const badge = qualityLabel(quality)!;
  if (quality.complete === false) return h("div", { class: "quality-report" }, h("span", { class: "pill" }, badge.label), h("p", null, quality.summary));
  const coverage = quality.lyrics.orderedCoverage;
  const knownLyrics = quality.lyrics.status === "pass" || quality.lyrics.status === "warning";
  const processing = processingText(quality.processing);
  const retries = [...new Set(quality.retryReasons.map(qualityRetryReason))];
  const preparation = quality.preparation;
  const extended = preparation && typeof preparation.requestedDurationSeconds === "number" && typeof preparation.effectiveDurationSeconds === "number"
    && preparation.effectiveDurationSeconds > preparation.requestedDurationSeconds;
  return h("div", { class: "quality-report" },
    h("div", { class: "row" }, h("span", { class: `pill tone-${badge.tone}` }, badge.label), version.recommended && h("span", { class: "pill tone-accent" }, "자동 추천"),
      h("span", { class: "footnote" }, `${quality.attempt}번째 시도 · 최대 ${quality.maxAttempts}회`)),
    h("p", null, quality.summary),
    extended && h("p", { class: "hint" }, `가사를 다 담도록 길이를 ${lengthLabel(preparation.requestedDurationSeconds)}에서 ${lengthLabel(preparation.effectiveDurationSeconds)}로 늘렸어요.`),
    h("div", { class: "check-grid" },
      h("div", { class: "check" }, h("span", { class: "sublabel" }, "소리"), h("p", null, audioQualityText(quality))),
      h("div", { class: "check" }, h("span", { class: "sublabel" }, "가사"), h("p", null, lyricQualityText(quality)),
        knownLyrics && typeof coverage === "number" && Number.isFinite(coverage) && h("p", { class: "footnote" }, `작성한 순서대로 인식된 가사 ${(Math.max(0, Math.min(1, coverage)) * 100).toFixed(0)}%`)),
      processing && h("div", { class: "check" }, h("span", { class: "sublabel" }, "재생본 정리"), h("p", null, processing)),
    ),
    retries.length ? h("details", { class: "disclosure" }, h("summary", null, "자동 재시도 이유"), h("ul", { class: "plain-list" }, retries.map((reason) => h("li", null, reason)))) : null,
  );
}

function checksPanel(song: Song, version: Version): Child {
  const exports = song.exports.filter((item) => item.candidateId === version.id);
  return [
    qualityReport(version),
    h(
      "div",
      { class: `file-state ${version.fileOk ? "tone-ok" : "tone-danger"}` },
      icon(version.fileOk ? "check" : "close", 16),
      h("div", null, h("b", null, version.fileOk ? "음원 파일 확인됨" : "파일을 쓸 수 없어요"), h("p", null, version.fileOk ? "크기와 SHA-256이 만들 때 기록과 같아요." : version.fileMessage)),
      h("button", { type: "button", class: "button ghost small", disabled: !version.fileOk, onClick: () => reveal({ kind: "version", versionId: version.id }) }, icon("folder", 14), "Finder"),
    ),
    version.findings.length > 0 && h(
      "ul",
      { class: "findings" },
      version.findings.map((finding) => {
        const copy = findingCopy(finding);
        return h(
          "li",
          { class: `finding tone-${finding.severity}` },
          h("div", { class: "finding-head" }, h("b", null, copy.label), copy.value && h("span", { class: "mono" }, copy.value)),
          h("p", null, copy.message),
          finding.startSeconds != null && finding.endSeconds != null
            ? h("button", {
                type: "button", class: "button small secondary", disabled: !version.fileOk,
                onClick: () => {
                  const start = Math.max(0, finding.startSeconds! - 0.25);
                  const end = Math.min(version.durationSeconds ?? Infinity, finding.endSeconds! + 0.25);
                  set({ selection: { startSeconds: start, endSeconds: end }, scope: "range", listenToParent: false, loop: true });
                  player?.seek(start);
                  if (player && !player.playing) player.toggle();
                },
              }, `${clock(finding.startSeconds, true, 2)}–${clock(finding.endSeconds, true, 2)} 듣기`)
            : null,
        );
      }),
    ),
    exports.length
      ? h("div", { class: "exports" },
          h("span", { class: "sublabel" }, "내보낸 파일"),
          exports.map((item) =>
            h("div", { class: "export-row" },
              h("span", null, relativeTime(item.createdAt), item.externalPath ? ` · ${item.externalPath.split("/").pop()}` : ""),
              h("button", { type: "button", class: "button ghost small", disabled: !(item.externalExists || item.exists), onClick: () => reveal({ kind: "export", artifactId: item.artifactId }) }, icon("folder", 14), "보기"),
            )),
        )
      : null,
    h("p", { class: "footnote" }, "자동 검사는 측정값과 인식한 가사로만 판단해요. 곡의 매력은 직접 들어 보고 평가해 주세요."),
  ];
}

function notesPanel(song: Song, version: Version): Child {
  if (!els) return null;
  const review = version.review;
  const verdicts: Array<[ReviewStatus, string, string]> = [
    ["approved", "좋아요", "thumbUp"],
    ["listened", "들어 봤어요", "check"],
    ["rejected", "별로예요", "thumbDown"],
  ];
  const noteArea = els.noteArea;
  return [
    h(
      "div",
      { class: "verdicts" },
      verdicts.map(([status, label, iconName]) =>
        h(
          "button",
          {
            type: "button",
            class: `verdict tone-${status}${review.status === status ? " is-active" : ""}`,
            "aria-pressed": review.status === status ? "true" : "false",
            onClick: () => void saveReview(song.songId, version, review.status === status ? "unreviewed" : status, review.status === status ? null : review.rating),
          },
          icon(iconName, 18),
          label,
        ),
      ),
      h(
        "div",
        { class: "stars", role: "radiogroup", "aria-label": "별점" },
        [1, 2, 3, 4, 5].map((value) =>
          h(
            "button",
            {
              type: "button",
              role: "radio",
              "aria-checked": review.rating === value ? "true" : "false",
              "aria-label": `${value}점`,
              class: `star${review.rating && value <= review.rating ? " is-on" : ""}`,
              onClick: () => void saveReview(song.songId, version, review.status === "unreviewed" ? "listened" : review.status, value),
            },
            icon("star", 17),
          ),
        ),
      ),
    ),
    h("div", { class: "note-compose" },
      noteArea,
      h("button", {
        type: "button",
        class: "button secondary",
        onClick: async () => {
          const note = noteArea.value.trim();
          if (!note) {
            noteArea.focus();
            return;
          }
          const saved = await saveReview(song.songId, version, review.status, review.rating, note);
          if (saved && get().activeVersionId === version.id && noteArea.value.trim() === note) noteArea.value = "";
        },
      }, "메모 남기기"),
    ),
    review.notes.length
      ? h("ol", { class: "notes" }, [...review.notes].reverse().map((note) => h("li", null, h("time", null, relativeTime(note.createdAt)), h("p", null, note.text))))
      : null,
    h("p", { class: "footnote" }, "자동 검사와 따로 저장돼요. 검사를 통과해도 직접 듣기 전에는 ‘아직 안 들음’으로 남아요."),
  ];
}

// ---------------------------------------------------------------------------
// Fix panel
// ---------------------------------------------------------------------------

function renderFix(state: State, song: Song): void {
  if (!els) return;
  const version = activeVersion();
  const current = node(version?.id);
  const range = state.selection;
  const scope = range ? state.scope : "song";
  mount(els.fixTarget, current ? ["고칠 버전: ", h("b", null, current.short)] : "버전을 먼저 고르세요");
  mount(
    els.fixScope,
    h(
      "div",
      { class: "segmented block" },
      h("button", { type: "button", class: scope === "song" ? "is-active" : "", "aria-pressed": scope === "song" ? "true" : "false", onClick: () => set({ scope: "song", plan: null }) }, "곡 전체"),
      h("button", {
        type: "button",
        class: scope === "range" ? "is-active" : "",
        disabled: !range,
        "aria-pressed": scope === "range" ? "true" : "false",
        "data-tip": range ? "" : "파형을 끌어서 구간을 먼저 고르세요",
        onClick: () => set({ scope: "range", plan: null }),
      }, range ? `구간 ${rangeLabel(range)}` : "구간만"),
    ),
    h("p", { class: "hint" }, scope === "range"
      ? "고른 구간만 다시 만들고 나머지는 그대로 둬요."
      : "요청을 반영한 새 전체 버전을 만들어요. 지금 버전은 그대로 남아요."),
  );

  const rules = state.rules.filter((rule) => QUICK_RULES.includes(rule.key)).sort((a, b) => QUICK_RULES.indexOf(a.key) - QUICK_RULES.indexOf(b.key));
  const visible = showAllChips ? rules : rules.slice(0, CHIPS_SHOWN);
  mount(
    els.fixChips,
    visible.map((rule) =>
      h(
        "button",
        {
          type: "button",
          class: "chip small",
          "data-tip": `“${rule.example}”을 적어 넣어요`,
          onClick: () => {
            if (!els) return;
            const text = els.fixArea.value.trim();
            els.fixArea.value = text ? `${text}, ${rule.example}` : rule.example;
            set({ feedback: els.fixArea.value });
            els.fixArea.focus();
          },
        },
        rule.label,
      ),
    ),
    rules.length > CHIPS_SHOWN &&
      h("button", { type: "button", class: "chip small ghost", onClick: () => { showAllChips = !showAllChips; renderFix(get(), song); } }, showAllChips ? "접기" : `+${rules.length - CHIPS_SHOWN}`),
  );
  if (els.fixArea.value !== state.feedback) els.fixArea.value = state.feedback;
  renderInstrumentChoices(version);

  mount(els.fixOptions, scope === "range"
    ? [h("span", { class: "sublabel" }, "얼마나 바꿀까요"),
      h("div", { class: "segmented small" }, (["light", "medium", "strong"] as Strength[]).map((key) =>
        h("button", { type: "button", class: state.strength === key ? "is-active" : "", "aria-pressed": state.strength === key ? "true" : "false", "data-tip": strengthCopy[key].hint, onClick: () => set({ strength: key, plan: null }) }, strengthCopy[key].label)))]
    : [h("span", { class: "sublabel" }, "새로 만들 버전"),
      h("div", { class: "segmented small" }, [1, 2, 3, 4].map((count) =>
        h("button", { type: "button", class: state.planVersions === count ? "is-active" : "", "aria-pressed": state.planVersions === count ? "true" : "false", onClick: () => set({ planVersions: count, plan: null }) }, `${count}개`)))]);

  const assistant = state.settings.assistant;
  els.assistantLabel.textContent = assistant.kind === "rules" ? "해석: 기본 규칙 · 바꾸기" : `해석: 로컬 LLM · ${assistant.model || "모델 미선택"}`;
  els.assistantLabel.dataset.tip = "요청을 해석하는 AI 도우미를 설정에서 바꿔요";
  // withBusy owns the disabled state while a plan is being made.
  if (!state.planning) els.planButton.disabled = !version;
  renderPlan(state, song);
  renderHistory(song);
}

async function requestPlan(): Promise<void> {
  if (!els || get().planning || els.planButton.disabled) return;
  const state = get();
  const version = activeVersion();
  if (!version) return;
  const feedback = els.fixArea.value.trim();
  const instruments = instrumentVersion === version.id ? new Map(instrumentChanges) : new Map<string, "add" | "remove">();
  if (!feedback && !instruments.size) {
    toast("무엇이 마음에 안 드는지 한 줄 적거나, 아래에서 악기를 골라 주세요.", { tone: "error" });
    els.fixArea.focus();
    return;
  }
  const ticket = planTicket();
  if (!ticket) return;
  const range = state.scope === "range" && state.selection ? { ...state.selection } : null;
  if (!feedback) {
    // Instruments alone need no interpretation: build the change here and show it.
    set({ plan: withInstruments(baselinePlan(version, range, state), instruments) });
    els.planBox.scrollIntoView({ block: "nearest", behavior: "smooth" });
    return;
  }
  set({ planning: true, plan: null });
  const label = state.settings.assistant.kind === "rules" ? "해석하는 중" : "LLM이 해석하는 중";
  const answer = await withBusy(els.planButton, label, () =>
    api.plan({ songId: ticket.song.songId, versionId: version.id, feedback, range, strength: state.strength, versions: state.planVersions }),
  );
  const plan = answer && instruments.size ? withInstruments(answer, instruments) : answer;
  const fresh = isPlanTicket(ticket);
  set({ planning: false, ...(fresh ? { plan: plan ?? null } : {}) });
  if (fresh && plan) els?.planBox.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// The style the fix assistant starts from: the version's own words, or its preset.
function baseStyle(version: Version): string {
  return version.baseStylePrompt || version.productionRules?.preset?.caption || version.stylePrompt;
}

function baselinePlan(version: Version, range: Plan["range"], state: State): Plan {
  const base = baseStyle(version);
  return {
    action: range ? "repaint" : "regenerate",
    summary: "",
    stylePrompt: base,
    baseStylePrompt: base,
    lyrics: null,
    bpm: null,
    range,
    strength: state.strength,
    versions: state.planVersions,
    changes: [],
    assistant: "rules",
    assistantModel: null,
    understood: true,
    notes: [],
    candidateId: version.id,
  };
}

function withInstruments(plan: Plan, instruments: Map<string, "add" | "remove">): Plan {
  const result = applyInstrumentChanges(plan.stylePrompt, instruments);
  const named = INSTRUMENTS.filter((item) => instruments.has(item.tag)).map((item) => `${item.label} ${instruments.get(item.tag) === "add" ? "더하기" : "빼기"}`);
  const stuck = INSTRUMENTS.filter((item) => instruments.get(item.tag) === "remove" && !result.changes.some((change) => change.label === item.label));
  const notes = [
    ...plan.notes,
    plan.range ? "고른 구간에서만 악기가 바뀌어요. 앞뒤와 자연스럽게 이어지는지 비교해 들어 보세요." : "악기를 바꾼 스타일로 곡 전체를 새로 만들어요. 선율도 달라질 수 있어요.",
    ...stuck.map((item) => `‘${item.label}’ 태그는 장르 추천 안내에 들어 있어 이 방법으로는 뺄 수 없어요.`),
  ];
  return {
    ...plan,
    summary: [plan.summary, `악기: ${named.join(", ")}`].filter(Boolean).join(" · "),
    stylePrompt: result.style,
    changes: [...plan.changes, ...result.changes],
    notes,
  };
}

function renderInstrumentChoices(version: Version | null): void {
  if (!els) return;
  if ((version?.id ?? null) !== instrumentVersion) {
    instrumentChanges = new Map();
    instrumentVersion = version?.id ?? null;
  }
  const style = version?.stylePrompt ?? "";
  const editable = version ? baseStyle(version) : "";
  const details = els.fixInstruments;
  mount(
    details,
    h("summary", null, "악기 더하기·빼기 ", h("em", null, instrumentChanges.size ? `${instrumentChanges.size}개 고름` : "선택")),
    h("div", { class: "chips" }, INSTRUMENTS.map((instrument) => {
      const present = hasInstrument(style, instrument);
      // Heard in this version only through the genre preset's own wording: not removable here.
      const locked = present && !hasInstrument(editable, instrument);
      const op = instrumentChanges.get(instrument.tag);
      return h("button", {
        type: "button",
        class: `chip small instrument${present ? " is-present" : ""}${locked ? " is-locked" : ""}${op ? ` op-${op}` : ""}`,
        "aria-pressed": op ? "true" : "false",
        "aria-disabled": locked ? "true" : undefined,
        disabled: !version,
        "data-tip": op ? "고른 것을 취소해요" : locked ? "장르 추천 안내에 들어 있는 악기라 여기서는 뺄 수 없어요" : present ? `이 버전에 있어요 · 누르면 빼요 (${instrument.tag})` : `누르면 더해요 (${instrument.tag})`,
        onClick: () => {
          if (locked && !op) return;
          if (op) instrumentChanges.delete(instrument.tag);
          else instrumentChanges.set(instrument.tag, present ? "remove" : "add");
          if (get().plan) set({ plan: null });
          renderInstrumentChoices(version);
        },
      }, op === "add" ? `+ ${instrument.label}` : op === "remove" ? `− ${instrument.label}` : instrument.label);
    })),
    h("p", { class: "hint" }, "밝게 보이는 악기는 이 버전에 들어 있어요. 고른 뒤 ‘수정안 보기’를 누르면 바뀔 태그를 먼저 보여 드려요."),
  );
  if (instrumentChanges.size) details.open = true;
}

function planActionText(plan: Plan): string {
  if (plan.action === "repaint" && plan.range) return `${rangeLabel(plan.range)} 구간만 다시 · 변화 ${strengthCopy[plan.strength].label}`;
  return `곡 전체를 새로 · 버전 ${plan.versions}개`;
}

function renderPlan(state: State, song: Song): void {
  if (!els) return;
  const plan = state.plan;
  const context = planTicket();
  if (!plan || plan.candidateId !== state.activeVersionId || !context) {
    els.planBox.replaceChildren();
    return;
  }
  const updatePlan = (change: Partial<Plan>) => {
    if (isPlanTicket(context) && get().plan) set({ plan: { ...get().plan!, ...change } });
  };
  const busy = songBusy();
  const ready = engineReady();
  const caption = h("textarea", {
    class: "input mono-input",
    rows: 4,
    value: plan.stylePrompt,
    onChange: (event: Event) => updatePlan({ stylePrompt: (event.target as HTMLTextAreaElement).value }),
  });
  mount(
    els.planBox,
    h(
      "div",
      { class: "plan-card" },
      h("div", { class: "plan-head" }, h("span", { class: "plan-eyebrow" }, "수정안"), h("span", { class: "pill" }, plan.assistant === "llm" ? `LLM · ${plan.assistantModel}` : "기본 규칙")),
      h("p", { class: "plan-summary" }, plan.summary),
      h("div", { class: "plan-action" }, icon(plan.action === "repaint" ? "branch" : "refresh", 15), h("span", null, planActionText(plan))),
      plan.changes.length
        ? h("div", { class: "plan-changes" },
            h("span", { class: "sublabel" }, "바꿀 태그 ", h("em", null, "눌러서 빼거나 되살려요")),
            h("div", { class: "chips" }, plan.changes.map((change) => {
              const applied = change.op === "add" ? hasTag(plan.stylePrompt, change.term) : !hasTag(plan.stylePrompt, change.term);
              return h("button", {
                type: "button",
                class: `tag-chip op-${change.op}${applied ? "" : " is-reverted"}`,
                "aria-pressed": applied ? "true" : "false",
                "data-tip": applied ? (change.op === "add" ? "이 태그를 빼요" : "이 태그를 되살려요") : "다시 적용해요",
                onClick: () => updatePlan({ stylePrompt: toggleTag(plan.stylePrompt, change.term) }),
              }, change.op === "add" ? "+ " : "− ", change.term, change.label && h("small", null, change.label));
            })),
          )
        : null,
      plan.notes.map((note) => h("p", { class: "plan-note" }, icon("info", 14), note)),
      plan.bpm ? h("p", { class: "plan-note" }, icon("info", 14), `빠르기도 ${plan.bpm} BPM으로 바꿔요.`) : null,
      plan.lyrics && h("details", { class: "disclosure" }, h("summary", null, "바뀐 가사 보기"), h("pre", { class: "lyrics-view" }, plan.lyrics)),
      h("details", { class: "disclosure" }, h("summary", null, "보낼 스타일 프롬프트 직접 고치기"), caption),
      h(
        "div",
        { class: "plan-buttons" },
        h(
          "button",
          {
            type: "button",
            class: "button primary",
            disabled: busy || !ready,
            "data-tip": ready ? (busy ? "지금 작업이 끝난 뒤 만들 수 있어요" : "") : "음악 엔진을 켜야 만들 수 있어요",
            onClick: (event: Event) =>
              void withBusy(event.currentTarget as HTMLButtonElement, "시작하는 중", async () => {
                const applyingPlan = get().plan;
                if (!isPlanTicket(context) || !applyingPlan) return;
                const next = await api.applyPlan(song.songId, applyingPlan, get().feedback.trim());
                if (!applySongForContext(next, context.song)) return;
                if (isPlanTicket(context)) {
                  if (els) els.fixArea.value = "";
                  instrumentChanges = new Map();
                  set({ plan: null, feedback: "" });
                }
                toast(plan.action === "repaint" ? "구간을 다시 만들기 시작했어요. 끝나면 새 수정본이 열려요." : `새 전체 버전 ${plan.versions}개를 만들기 시작했어요.`, { tone: "ok" });
              }),
          },
          "이대로 만들기",
        ),
        !ready && ["offline", "failed"].includes(get().engine.state) && h("button", { type: "button", class: "button secondary", onClick: () => void startEngine() }, icon("power", 14), "엔진 켜기"),
        h("button", { type: "button", class: "button ghost", onClick: () => set({ plan: null }) }, "닫기"),
      ),
    ),
  );
}

function renderHistory(song: Song): void {
  if (!els) return;
  const records = [...song.feedback].reverse();
  if (!records.length) {
    els.history.replaceChildren();
    return;
  }
  const running = get().task?.songId === song.songId;
  mount(
    els.history,
    h("h3", { class: "sublabel" }, `고친 기록 ${records.length}`),
    h(
      "ol",
      { class: "history-list" },
      records.map((record) => {
        const job = song.jobs.find((item) => item.jobId === record.jobId);
        const results = song.versions.filter((item) => item.feedbackId === record.feedbackId);
        const source = node(record.candidateId);
        let status: Child = null;
        if (!results.length) {
          if (job && ["running", "queued"].includes(job.status)) status = h("span", { class: "muted" }, running ? "만드는 중" : "멈춤");
          else if (job?.status === "interrupted") status = h("span", { class: "muted" }, "중단됨");
          else if (job?.status === "cancelled") status = h("span", { class: "muted" }, "취소함");
          else if (job && ["failed", "partial"].includes(job.status)) status = h("span", { class: "tone-danger-text" }, "실패");
        }
        return h(
          "li",
          { class: "history-item" },
          h("p", { class: "history-text" }, `“${record.text || "요청 없이 다시 만들기"}”`),
          h("p", { class: "history-meta" }, [source?.short, record.range ? rangeLabel(record.range) : "곡 전체", relativeTime(record.createdAt)].filter(Boolean).join(" · "),
            results.map((item) => h("button", { type: "button", class: "link", onClick: () => selectVersion(item.id) }, ` → ${node(item.id)?.short ?? "새 버전"}`)),
            status && [" · ", status],
          ),
        );
      }),
    ),
  );
}

// ---------------------------------------------------------------------------
// Dialogs and exports
// ---------------------------------------------------------------------------

function openRegenerateSong(song: Song, version: Version, sourceName: string): void {
  const ticket = songTicket(song.songId);
  if (!ticket || !version.fileOk || songBusy()) return;
  const initialStyle = version.baseStylePrompt ?? version.stylePrompt;
  const initialLyrics = version.lyricsOriginal ?? version.lyrics;
  const style = h("textarea", { class: "input mono-input", rows: 3, maxlength: 1500, value: initialStyle, placeholder: "비워 두면 이 버전의 제작 규칙을 사용해요" });
  const lyrics = h("textarea", { class: "input lyrics-input", rows: 8, maxlength: 4096, value: initialLyrics });
  const versions = h("select", { class: "input", "aria-label": "새 버전 수" }, [1, 2, 3, 4].map((count) => h("option", { value: String(count), selected: count === 1 }, `${count}개`)));
  const button = h("button", { type: "button", class: "button primary", disabled: !engineReady(), onClick: (event: Event) => void withBusy(event.currentTarget as HTMLButtonElement, "준비하는 중", async () => {
    if (!isSongTicket(ticket)) throw new Error("열린 곡이 바뀌었어요. 현재 곡에서 다시 시도하세요.");
    const changedLyrics = lyrics.value.trim();
    if (!changedLyrics) { lyrics.focus(); throw new Error("가사를 적거나 [Instrumental]을 써 주세요."); }
    const result = await api.regenerateSong({ songId: song.songId, versionId: version.id, versions: Number(versions.value),
      ...(style.value === initialStyle ? {} : { stylePrompt: style.value.trim() }),
      ...(lyrics.value === initialLyrics ? {} : { lyrics: changedLyrics }) });
    if (!applySongForContext(result, ticket)) return;
    dialog.close();
    toast(`「${sourceName}」의 가사·편곡으로 새 전체 버전을 만들기 시작했어요.`, { tone: "ok" });
  }) }, "새 전체 버전 만들기");
  const dialog = h("dialog", { class: "dialog wide" }, h("form", { method: "dialog", class: "dialog-body" },
    h("h2", { class: "dialog-title" }, "가사·편곡으로 새로 만들기"),
    h("p", { class: "dialog-text" }, `「${sourceName}」의 가사와 편곡으로 새 전체 곡을 만들어요. 선율과 목소리는 달라질 수 있고, 원본은 그대로 남아요.`),
    h("label", { class: "field" }, h("span", { class: "label" }, "스타일"), style),
    h("label", { class: "field" }, h("span", { class: "label" }, "가사"), lyrics),
    h("label", { class: "field" }, h("span", { class: "label" }, "만들 버전"), versions),
    h("div", { class: "dialog-actions" }, h("button", { type: "submit", value: "cancel", class: "button ghost" }, "취소"), button),
  ));
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.showModal();
}

async function exportFinal(button: HTMLButtonElement, songId: string): Promise<void> {
  const ticket = songTicket(songId);
  if (!ticket) return;
  const result = await withBusy(button, "내보내는 중", () => api.exportFinal(songId));
  if (!result || !applySongForContext(result.state, ticket)) return;
  const artifact = result.state.song?.exports.at(-1);
  toast(`WAV로 내보냈어요: ${result.path.split("/").pop()}`, {
    tone: "ok",
    action: artifact ? { label: "Finder에서 보기", run: () => reveal({ kind: "export", artifactId: artifact.artifactId }) } : undefined,
  });
}

function openSongSettings(song: Song): void {
  const ticket = songTicket(song.songId);
  if (!ticket) return;
  const versions = get().settings.defaultVersions;
  const title = h("input", { class: "input", value: song.title, maxlength: 100 });
  const style = h("textarea", { class: "input mono-input", rows: 3, value: song.inputs.stylePrompt, maxlength: 1500 });
  const lyrics = h("textarea", { class: "input lyrics-input", rows: 10, value: song.inputs.lyrics, maxlength: 4096 });
  const duration = h("input", { class: "input", type: "number", min: "10", max: "300", step: "5", value: String(Math.round(song.inputs.durationSeconds)) });
  const bpm = h("input", { class: "input", type: "number", min: "30", max: "300", placeholder: "엔진에 맡기기", value: song.inputs.bpm ? String(song.inputs.bpm) : "" });
  const dialog = h(
    "dialog",
    { class: "dialog wide" },
    h(
      "form",
      { method: "dialog", class: "dialog-body" },
      h("h2", { class: "dialog-title" }, "곡 설정 고치기"),
      h("p", { class: "dialog-text" }, "다음에 만드는 버전부터 적용돼요. 이미 만든 버전과 기록은 그대로 남아요."),
      h("label", { class: "field" }, h("span", { class: "label" }, "제목"), title),
      h("label", { class: "field" }, h("span", { class: "label" }, "스타일"), style),
      h("label", { class: "field" }, h("span", { class: "label" }, "가사"), lyrics),
      h("div", { class: "field-row" }, h("label", { class: "field" }, h("span", { class: "label" }, "길이 (초)"), duration), h("label", { class: "field" }, h("span", { class: "label" }, "빠르기 (BPM)"), bpm)),
      h(
        "div",
        { class: "dialog-actions" },
        h("button", { type: "submit", value: "cancel", class: "button ghost" }, "취소"),
        h("button", { type: "submit", value: "save", class: "button secondary" }, "저장"),
        h("button", { type: "submit", value: "generate", class: "button primary", disabled: !engineReady() }, `저장하고 버전 ${versions}개 만들기`),
      ),
    ),
  );
  document.body.append(dialog);
  dialog.addEventListener("close", async () => {
    const action = dialog.returnValue;
    dialog.remove();
    if (action !== "save" && action !== "generate") return;
    if (!isSongTicket(ticket)) {
      toast("열린 곡이 바뀌었어요. 현재 곡에서 다시 시도하세요.", { tone: "error" });
      return;
    }
    try {
      const bpmValue = bpm.value.trim() ? Number.parseInt(bpm.value, 10) : null;
      let next = await api.revise({
        songId: song.songId,
        title: title.value,
        stylePrompt: style.value,
        lyrics: lyrics.value,
        durationSeconds: Number(duration.value),
        bpm: bpmValue,
      });
      if (!applySongForContext(next, ticket)) return;
      if (action === "generate") {
        next = await api.generateMore(song.songId, versions);
        if (!applySongForContext(next, ticket)) return;
        toast(`새 설정으로 버전 ${versions}개를 만들기 시작했어요.`, { tone: "ok" });
      } else {
        toast("곡 설정을 저장했어요. 다음 버전부터 적용돼요.", { tone: "ok" });
      }
    } catch (error) {
      toast(errorText(error), { tone: "error" });
    }
  });
  dialog.showModal();
}

// ---------------------------------------------------------------------------
// Render entry
// ---------------------------------------------------------------------------

let lastTaskShape = "";

// Progress ticks arrive every second; only the banner follows them. Everything else
// re-renders when the task starts, finishes a version, or stops, so text being typed
// in the fix panel never loses focus mid-generation.
function taskShape(state: State, song: Song): string {
  const task = state.task && state.task.songId === song.songId ? state.task : null;
  return task ? `${task.kind}|${task.done}|${task.total}|${task.cancelling}` : "";
}

export function renderStudio(changed: Set<keyof State>): void {
  const state = get();
  const song = state.song.song;
  if (!song) return;
  if (!els || !els.head.isConnected) {
    build();
    changed = new Set(Object.keys(state) as Array<keyof State>);
  }
  const shape = taskShape(state, song);
  const taskMoved = changed.has("task") && shape !== lastTaskShape;
  lastTaskShape = shape;
  const all = changed.has("song") || changed.has("view");
  const versionChanged = all || changed.has("activeVersionId") || changed.has("listenToParent") || changed.has("comparisonVersionId");
  if (all || changed.has("engine") || taskMoved || changed.has("activeVersionId")) renderHead(song);
  if (all || changed.has("engine") || taskMoved || versionChanged || changed.has("settings")) renderVersions(state, song);
  if (all || changed.has("task") || changed.has("engine")) renderBanner(state, song);
  if (versionChanged || taskMoved) renderPlayer(state, song);
  if (changed.has("selection") || changed.has("loop")) {
    if (player) {
      player.loop = state.loop;
      player.setSelection(state.selection);
    }
    renderSelectionBar(state);
  }
  const lyricsInputs = lyricsEdit && state.tab === "lyrics" && (changed.has("selection") || changed.has("strength") || changed.has("engine") || taskMoved);
  if (versionChanged || changed.has("tab") || lyricsInputs) {
    renderTabs(state);
    renderInfo(state, song);
  }
  const fixKeys: Array<keyof State> = ["selection", "scope", "strength", "planVersions", "feedback", "plan", "planning", "settings", "rules", "engine"];
  if (versionChanged || taskMoved || fixKeys.some((key) => changed.has(key))) renderFix(state, song);
}

// Screenshot fixture only (query ?demo=…): open an edit, select its range and ask for a plan.
export async function demoPlan(feedback: string, pickEdit: boolean): Promise<void> {
  const song = get().song.song;
  const edit = pickEdit ? song?.versions.find((item) => item.editRange) : null;
  if (edit?.editRange) set({ activeVersionId: edit.id, selection: edit.editRange, scope: "range" });
  if (!els) return;
  els.fixArea.value = feedback;
  set({ feedback });
  await requestPlan();
}

// Screenshot fixture only (query ?demo=lyrics): select a range and rewrite one line.
export function demoLyricsEdit(): void {
  const version = activeVersion();
  if (!els || !version?.durationSeconds) return;
  set({ tab: "lyrics", selection: { startSeconds: version.durationSeconds * 0.2, endSeconds: version.durationSeconds * 0.3 }, scope: "range" });
  startLyricsEdit(version);
  const lines = els.lyricsArea.value.split("\n");
  const index = lines.findIndex((line) => line.trim() && !line.trim().startsWith("["));
  if (index >= 0) lines[index] = `${lines[index]} (고친 줄)`;
  els.lyricsArea.value = lines.join("\n");
  syncLyricsActions();
}

// Screenshot fixtures only: ?demo=zoom frames a selection; ?demo=instruments asks for a
// piano-out, electric-guitar-in change without any request text.
export function demoZoom(): void {
  const version = activeVersion();
  if (!player || !version?.durationSeconds) return;
  set({ selection: { startSeconds: version.durationSeconds * 0.31, endSeconds: version.durationSeconds * 0.36 }, scope: "range" });
  player.fitSelection();
}

export function demoInstruments(): void {
  const version = activeVersion();
  if (!els || !version) return;
  renderInstrumentChoices(version);
  for (const instrument of INSTRUMENTS) {
    if (instrument.tag === "piano" && hasInstrument(version.stylePrompt, instrument)) instrumentChanges.set(instrument.tag, "remove");
    if (instrument.tag === "electric guitar") instrumentChanges.set(instrument.tag, "add");
  }
  renderInstrumentChoices(version);
  void requestPlan();
}

export function pausePlayback(): void {
  if (player?.playing) player.audio.pause();
}
