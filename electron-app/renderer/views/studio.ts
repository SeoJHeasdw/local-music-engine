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
import { confirmDialog, errorText, toast, withBusy } from "../ui.ts";
import { audioQualityText, isAutomaticAttempt, lyricQualityText, processingText, qualityLabel, qualityRetryReason } from "../quality.ts";
import { songPlanView } from "../song-plan-view.ts";
import { comparisonChoices, comparisonVersion } from "../comparison.ts";

type Els = {
  head: HTMLElement;
  banner: HTMLElement;
  rail: HTMLElement;
  playerHead: HTMLElement;
  canvas: HTMLCanvasElement;
  now: HTMLElement;
  total: HTMLElement;
  play: HTMLButtonElement;
  loop: HTMLButtonElement;
  selectionBar: HTMLElement;
  finalRow: HTMLElement;
  inputs: HTMLElement;
  tabs: HTMLElement;
  panels: Record<InspectorTab, HTMLElement>;
  fixScope: HTMLElement;
  fixChips: HTMLElement;
  fixArea: HTMLTextAreaElement;
  fixOptions: HTMLElement;
  planButton: HTMLButtonElement;
  assistantLabel: HTMLElement;
  planBox: HTMLElement;
  history: HTMLElement;
  review: HTMLElement;
  noteArea: HTMLTextAreaElement;
  details: HTMLElement;
};

let els: Els | null = null;
let player: Player | null = null;
let treeCache: { song: Song; tree: ReturnType<typeof versionTree> } | null = null;
let showAllChips = false;

const QUICK_RULES = ["diction", "vocal-forward", "ending", "transition", "calmer", "energetic", "drums-soft", "clean", "brighter", "emotional", "chorus", "faster", "slower", "variety"];

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

export function toggleLoop(): void {
  set({ loop: !get().loop });
}

export function clearSelection(): boolean {
  if (!get().selection) return false;
  set({ selection: null, scope: "song", loop: false });
  return true;
}

export function focusFeedback(): void {
  set({ tab: "fix" });
  els?.fixArea.focus();
}

function selectVersion(id: string): void {
  if (id === get().activeVersionId) return;
  set({ activeVersionId: id, listenToParent: false, plan: null });
}

// ---------------------------------------------------------------------------
// Build once
// ---------------------------------------------------------------------------

function build(): void {
  const root = byId("view-studio");
  const canvas = h("canvas", { class: "wave", "aria-label": "파형. 끌어서 구간을 고르고, 클릭해서 위치를 옮겨요." });
  const now = h("span", { class: "mono clock-now" }, "0:00.0");
  const total = h("span", { class: "mono clock-total" }, "0:00.0");
  const play = h("button", { type: "button", class: "transport-play", "aria-label": "재생", "data-tip": "재생 / 멈춤 (Space)", onClick: () => togglePlay() }, icon("play", 22));
  const loop = h("button", { type: "button", class: "button ghost small loop-toggle", "aria-pressed": "false", "data-tip": "고른 구간만 반복해서 들어요 (L)", onClick: () => toggleLoop() }, icon("loop", 14), "반복 재생");
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
    class: "input",
    rows: 4,
    maxlength: 2000,
    placeholder: "들은 그대로 적어 주세요.\n예: 후렴에서 가사가 잘 안 들리고 드럼이 너무 세요",
    onInput: (event: Event) => set({ feedback: (event.target as HTMLTextAreaElement).value }),
    onKeydown: (event: KeyboardEvent) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        void requestPlan();
      }
    },
  });
  const noteArea = h("textarea", { class: "input", rows: 3, maxlength: 2000, placeholder: "기억해 둘 점. 예: 1:12 가사 ‘기억해’가 ‘기억개’로 들림" });
  const planButton = h("button", { type: "button", class: "button primary", onClick: () => void requestPlan() }, icon("wand", 16), "수정안 만들기");
  const panels = {
    fix: h("div", { class: "panel", role: "tabpanel", id: "panel-fix" }),
    review: h("div", { class: "panel", role: "tabpanel", id: "panel-review" }),
    details: h("div", { class: "panel", role: "tabpanel", id: "panel-details" }),
  };
  const refs = {
    head: h("header", { class: "studio-head" }),
    banner: h("div", { class: "studio-banner" }),
    rail: h("aside", { class: "rail", "aria-label": "버전" }),
    playerHead: h("div", { class: "player-head" }),
    canvas,
    now,
    total,
    play,
    loop,
    selectionBar: h("div", { class: "selection-bar" }),
    finalRow: h("div", { class: "final-row" }),
    inputs: h("section", { class: "card inputs-card" }),
    tabs: h("div", { class: "tabs", role: "tablist" }),
    panels,
    fixScope: h("div", { class: "fix-scope" }),
    fixChips: h("div", { class: "fix-chips" }),
    fixArea,
    fixOptions: h("div", { class: "fix-options" }),
    planButton,
    assistantLabel: h("button", { type: "button", class: "assistant-label link", onClick: () => go("settings") }),
    planBox: h("div", { class: "plan-box" }),
    history: h("div", { class: "history" }),
    review: h("div", { class: "review-body" }),
    noteArea,
    details: h("div", { class: "details-body" }),
  } satisfies Els;
  els = refs;

  mount(
    panels.fix,
    h("h2", { class: "panel-title" }, "무엇을 바꿀까요?"),
    refs.fixScope,
    refs.fixChips,
    fixArea,
    refs.fixOptions,
    h("div", { class: "plan-actions" }, planButton, refs.assistantLabel),
    refs.planBox,
    refs.history,
  );
  mount(panels.review, refs.review);
  mount(panels.details, refs.details);

  mount(
    root,
    refs.head,
    refs.banner,
    h(
      "div",
      { class: "studio-grid" },
      refs.rail,
      h(
        "section",
        { class: "stage" },
        h(
          "div",
          { class: "card player-card" },
          refs.playerHead,
          h("div", { class: "wave-wrap" }, canvas),
          h(
            "div",
            { class: "transport" },
            h(
              "div",
              { class: "transport-left" },
              h("button", { type: "button", class: "transport-icon", "aria-label": "처음으로", "data-tip": "처음으로", onClick: () => player?.seek(get().selection?.startSeconds ?? 0) }, icon("start", 18)),
              h("button", { type: "button", class: "transport-icon", "aria-label": "5초 뒤로", "data-tip": "5초 뒤로 (←)", onClick: () => nudge(-5) }, icon("back5", 20)),
              play,
              h("button", { type: "button", class: "transport-icon", "aria-label": "5초 앞으로", "data-tip": "5초 앞으로 (→)", onClick: () => nudge(5) }, icon("fwd5", 20)),
              h("span", { class: "clock" }, now, h("span", { class: "clock-sep" }, "/"), total),
            ),
            h("div", { class: "transport-right" }, h("label", { class: "volume-wrap", "data-tip": "음량" }, icon("volume", 16), volume)),
          ),
          refs.selectionBar,
          refs.finalRow,
        ),
        refs.inputs,
      ),
      h("aside", { class: "inspector" }, refs.tabs, panels.fix, panels.review, panels.details),
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
      els.play.replaceChildren(icon(playing ? "pause" : "play", 22));
      els.play.setAttribute("aria-label", playing ? "멈춤" : "재생");
    }
  };
  player.onSelection = (range) => {
    set({ selection: range, scope: range ? "range" : "song", ...(range ? {} : { loop: false }) });
  };
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
// Head, banners, rail
// ---------------------------------------------------------------------------

function renderHead(song: Song): void {
  if (!els) return;
  const t = tree();
  const final = node(song.finalVersionId);
  const recommended = node(song.recommendedVersionId);
  const exporting = final ?? recommended;
  const edits = song.versions.filter((item) => !isAutomaticAttempt(item) && item.kind === "edit").length;
  const covers = song.versions.filter((item) => !isAutomaticAttempt(item) && item.kind === "cover").length;
  const busy = songBusy();
  const exportButton = h(
    "button",
    {
      type: "button",
      class: "button primary",
      disabled: !exporting || busy,
      "data-tip": exporting ? `${exporting.name}${final ? "" : " · 자동 추천"}을 WAV 파일로 저장해요` : "버전이 준비되면 WAV로 저장할 수 있어요",
      onClick: (event: Event) => void exportFinal(event.currentTarget as HTMLButtonElement, song.songId),
    },
    icon("export", 16),
    "WAV 내보내기",
  );
  mount(
    els.head,
    h(
      "div",
      { class: "studio-title" },
      h("h1", null, song.title),
      h(
        "p",
        { class: "studio-meta" },
        [
          `버전 ${t?.roots.filter((item) => !isAutomaticAttempt(item.version)).length ?? 0}개`,
          edits ? `수정 ${edits}개` : null,
          covers ? `커버 ${covers}개` : null,
          final ? `최종본 ${final.name}` : recommended ? `자동 추천 ${recommended.name}` : "자동 검사 후 추천본을 골라요",
          `목표 ${lengthLabel(song.inputs.durationSeconds)}`,
        ]
          .filter(Boolean)
          .join(" · "),
      ),
    ),
    h(
      "div",
      { class: "head-actions" },
      h("button", { type: "button", class: "button ghost", disabled: busy, "data-tip": "제목·스타일·가사·길이를 고쳐요. 다음 버전부터 적용돼요.", onClick: () => openSongSettings(song) }, icon("settings", 16), "곡 설정"),
      h("button", { type: "button", class: "button ghost", "data-tip": "Finder에서 곡 폴더 보기", onClick: () => reveal({ kind: "song", songId: song.songId }) }, icon("folder", 16), "폴더 열기"),
      exportButton,
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
        h(
          "div",
          { class: "task-text" },
          h("b", null, task.label),
          h("span", null, task.stage, task.detail ? ` · ${task.detail}` : ""),
        ),
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
                body: "이미 끝난 버전은 남아 있어요. 엔진에 제출한 한 곡은 취소 후에도 끝까지 계산될 수 있어요.",
                confirm: "취소하기",
                danger: true,
              });
              if (ok) {
                try {
                  await api.cancelTask(task.songId, task.startedAt);
                } catch (error) {
                  toast(errorText(error), { tone: "error" });
                }
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
      h("button", { type: "button", class: "button small ghost", onClick: () => void refreshSong(song.songId) }, "상태 새로 고침"),
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
          h("p", null, h("b", null, `${label} · ${done}/${total}개 완료`)),
          h("p", null, "처음 요청한 가사·스타일로 이어 만들어요. 파일을 확인한 완료 버전은 그대로 사용해요."),
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
      h("p", null, "끝나지 않은 작업이 있어요. 원본과 완료한 버전은 남아 있어요. 구간 수정은 원본을 골라 다시 요청할 수 있어요."),
    ));
    return;
  }
  els.banner.replaceChildren();
}

function reviewMark(status: ReviewStatus): Child {
  if (status === "approved") return h("span", { class: "review-mark tone-ok", "data-tip": "좋아요" }, icon("thumbUp", 14));
  if (status === "rejected") return h("span", { class: "review-mark tone-danger", "data-tip": "별로예요" }, icon("thumbDown", 14));
  if (status === "listened") return h("span", { class: "review-mark", "data-tip": "들어 봤어요" }, icon("check", 14));
  return h("span", { class: "review-mark is-new", "data-tip": "아직 안 들음" });
}

function renderRail(state: State, song: Song): void {
  if (!els) return;
  const t = tree();
  if (!t) return;
  const task = state.task && state.task.songId === song.songId ? state.task : null;
  const busy = Boolean(task);
  const rows: Child[] = [];
  const attempts: Child[] = [];
  const placeholder = (label: string, depth: number) =>
    h("div", { class: `version-row is-pending depth-${depth}`, "aria-hidden": "true" }, h("span", { class: "version-glyph" }, h("span", { class: "task-spinner small" })), h("span", { class: "version-text" }, h("b", null, label), h("small", null, "만드는 중")));
  for (const item of t.flat) {
    const version = item.version;
    const active = version.id === state.activeVersionId;
    const quality = qualityLabel(version.quality);
    const row =
      h(
        "button",
        {
          type: "button",
          role: "option",
          "aria-selected": active ? "true" : "false",
          class: `version-row depth-${Math.min(item.depth, 3)}${active ? " is-active" : ""}${version.isFinal ? " is-final" : ""}${version.fileOk ? "" : " is-broken"}`,
          onClick: () => selectVersion(version.id),
        },
        h("span", { class: "version-glyph" }, isAutomaticAttempt(version) ? String(version.quality?.attempt ?? "·") : item.depth ? icon("branch", 14) : item.short.replace("버전 ", "")),
        h(
          "span",
          { class: "version-text" },
          h("b", null, item.short),
          h(
            "small",
            null,
            version.fileOk ? (version.editRange ? rangeLabel(version.editRange) : lengthLabel(version.durationSeconds)) : "파일 없음",
          ),
          quality && h("small", { class: `quality-caption tone-${quality.tone}` }, quality.label),
        ),
        version.recommended && h("span", { class: "recommend-badge" }, "추천"),
        version.isFinal ? h("span", { class: "final-badge" }, "최종본") : reviewMark(version.review.status),
      );
    (isAutomaticAttempt(version) ? attempts : rows).push(row);
    if (task?.kind === "repaint" && song.feedback.at(-1)?.candidateId === version.id && !song.versions.some((item) => item.feedbackId === song.feedback.at(-1)?.feedbackId)) {
      rows.push(placeholder(`수정 ${item.children.length + 1}`, Math.min(item.depth + 1, 3)));
    }
  }
  if (task && task.kind !== "repaint") {
    const remaining = Math.max(0, task.total - task.done);
    const ready = t.roots.filter((item) => !isAutomaticAttempt(item.version)).length;
    for (let index = 0; index < remaining; index += 1) rows.push(placeholder(`버전 ${ready + index + 1}`, 0));
  }
  const settings = state.settings;
  mount(
    els.rail,
    h("div", { class: "rail-head" }, h("h2", null, "버전"), h("span", { class: "rail-count" }, String(song.versions.filter((version) => !isAutomaticAttempt(version)).length))),
    rows.length
      ? h("div", { class: "rail-list", role: "listbox", "aria-label": "버전 목록" }, rows)
      : h("p", { class: "rail-empty" }, "아직 버전이 없어요."),
    attempts.length ? h("details", { class: "automatic-attempts", open: Boolean(activeVersion() && isAutomaticAttempt(activeVersion()!)) },
      h("summary", null, `자동 생성 이력 ${attempts.length}개`),
      h("p", { class: "footnote" }, "처음 만든 음원과 다시 만든 시도를 모두 남겨 두었어요."),
      h("div", { class: "rail-list", role: "listbox", "aria-label": "자동 생성 이력" }, attempts)) : null,
    h(
      "details",
      { class: "more-menu" },
      h("summary", { class: `button ghost block${busy || !engineReady() ? " is-disabled" : ""}`, "data-tip": engineReady() ? "지금 스타일 그대로 새 버전을 더 만들어요" : "음악 엔진을 켜야 만들 수 있어요" }, icon("plus", 16), "같은 설정으로 더 만들기"),
      h(
        "div",
        { class: "menu" },
        [1, 2, 4].map((count) =>
          h(
            "button",
            {
              type: "button",
              disabled: busy || !engineReady(),
              onClick: async (event: Event) => {
                (event.currentTarget as HTMLElement).closest("details")?.removeAttribute("open");
                try {
                  const ticket = songTicket(song.songId);
                  if (!ticket || !applySongForContext(await api.generateMore(song.songId, count), ticket)) return;
                  toast(`버전 ${count}개를 더 만들기 시작했어요.`, { tone: "ok" });
                } catch (error) {
                  toast(errorText(error), { tone: "error" });
                }
              },
            },
            `${count}개${count === settings.defaultVersions ? " (기본)" : ""}`,
          ),
        ),
      ),
    ),
  );
}

// ---------------------------------------------------------------------------
// Player card and inputs
// ---------------------------------------------------------------------------

function renderPlayer(state: State, song: Song): void {
  if (!els || !player) return;
  const current = node(state.activeVersionId);
  if (!current) {
    const making = state.task && state.task.songId === song.songId;
    mount(els.playerHead, h("h2", null, making ? "첫 버전을 만드는 중이에요" : "아직 버전이 없어요"));
    player.unavailable = making ? "버전이 하나씩 끝나는 대로 여기서 바로 들을 수 있어요" : "왼쪽 아래에서 버전을 만들어 보세요";
    void player.load(null);
    els.selectionBar.replaceChildren();
    els.finalRow.replaceChildren();
    return;
  }
  const version = current.version;
  const parent = node(versionSourceParent(song, version)?.id);
  const choices = comparisonChoices(song, version);
  const compare = node(comparisonVersion(song, version, state.comparisonVersionId)?.id);
  const listeningComparison = Boolean(state.listenToParent && compare);
  const strength = strengthFromValue(version.repaintStrength);
  mount(
    els.playerHead,
    h(
      "div",
      { class: "player-title" },
      h("h2", null, current.name),
      h(
        "div",
        { class: "player-badges" },
        version.isFinal && h("span", { class: "pill tone-accent" }, "최종본"),
        version.recommended && h("span", { class: "pill tone-accent" }, "자동 추천"),
        qualityLabel(version.quality) && h("span", { class: `pill tone-${qualityLabel(version.quality)!.tone}` }, qualityLabel(version.quality)!.label),
        version.editRange && parent && h("span", { class: "pill tone-edit" }, `${parent.short}의 ${rangeLabel(version.editRange)} 수정${strength ? ` · 변화 ${strengthCopy[strength].label}` : ""}`),
        version.kind === "cover" && parent && h("span", { class: "pill tone-edit" }, `${parent.short} 바탕 · 원본 따르기 ${Math.round((version.coverStrength ?? 0.7) * 100)}%`),
        h("span", { class: `pill tone-${version.review.status}` }, reviewCopy[version.review.status].label),
      ),
    ),
    choices.length ? h("div", { class: "comparison-controls" },
      h("label", { class: "comparison-picker" }, h("span", { class: "label" }, "비교할 버전"),
        h("select", { class: "input", "aria-label": "비교할 버전", onChange: (event: Event) => set({
          comparisonVersionId: (event.target as HTMLSelectElement).value || null, listenToParent: false,
        }) },
        h("option", { value: "", selected: !state.comparisonVersionId || !compare }, parent ? `생성 원본 · ${parent.short}` : "버전을 골라 주세요"),
        choices.map((item) => h("option", { value: item.id, selected: state.comparisonVersionId === item.id },
          `${node(item.id)?.name ?? "버전"}${item.review.status === "approved" ? " · 좋아요" : ""}`)),
        )),
      compare &&
      h(
        "div",
        { class: "segmented small compare", role: "group", "aria-label": "두 버전 번갈아 듣기", "data-tip": "같은 재생 위치에서 바꿔 들어요 (C). 두 곡의 전개 위치는 다를 수 있어요." },
        h("button", { type: "button", class: listeningComparison ? "is-active" : "", "aria-pressed": listeningComparison ? "true" : "false", onClick: () => set({ listenToParent: true }) }, `비교 · ${compare.short}`),
        h("button", { type: "button", class: listeningComparison ? "" : "is-active", "aria-pressed": listeningComparison ? "false" : "true", onClick: () => set({ listenToParent: false }) }, `현재 · ${current.short}`),
      ),
      h("p", { class: "hint" }, "번갈아 들으며 선율·감정·곡의 흐름을 비교해 보세요. 비교 버전은 재생에만 사용해요."),
    ) : null,
  );
  const source = listeningComparison ? compare!.version : version;
  player.editRange = version.editRange;
  player.overlay = listeningComparison ? `지금 듣는 버전: ${compare!.name}` : null;
  player.unavailable = source.fileOk ? null : "이 버전의 음원 파일을 쓸 수 없어요";
  player.selection = state.selection;
  player.loop = state.loop;
  void player.load({ key: source.id, url: source.audioUrl, peaks: source.waveform, duration: source.durationSeconds });

  renderSelectionBar(state);

  const busy = songBusy();
  mount(
    els.finalRow,
    version.isFinal
      ? [
          h("span", { class: "final-note" }, icon("check", 16), "이 버전이 최종본이에요"),
          song.canUndoFinal &&
            h(
              "button",
              {
                type: "button",
                class: "button ghost small",
                disabled: busy,
                onClick: (event: Event) =>
                  void withBusy(event.currentTarget as HTMLButtonElement, "되돌리는 중", async () => {
                    const ticket = songTicket(song.songId);
                    if (!ticket || !applySongForContext(await api.undoFinal(song.songId), ticket)) return;
                    toast("최종본 지정을 되돌렸어요.");
                  }),
              },
              icon("undo", 14),
              "지정 되돌리기",
            ),
        ]
      : [
          h(
            "button",
            {
              type: "button",
              class: "button secondary",
              disabled: busy || !version.fileOk,
              onClick: (event: Event) =>
                void withBusy(event.currentTarget as HTMLButtonElement, "지정하는 중", async () => {
                  const ticket = songTicket(song.songId);
                  if (!ticket || !applySongForContext(await api.setFinal(song.songId, version.id), ticket)) return;
                  toast(`${current.name}을 최종본으로 지정했어요.`, { tone: "ok" });
                }),
            },
            icon("check", 16),
            "최종본으로 지정",
          ),
          h("span", { class: "hint" }, !song.finalVersionId && song.recommendedVersionId ? "추천본은 바로 WAV로 저장할 수 있어요. 직접 최종본을 지정하면 그 버전이 우선해요." : "최종본은 WAV로 내보낼 버전이에요. 언제든 다른 버전으로 바꿀 수 있어요."),
        ],
    h("button", { type: "button", class: "button secondary", disabled: busy || !version.fileOk, onClick: () => openRegenerateSong(song, version, current.name) }, icon("wand", 16), "가사·편곡으로 새로 만들기"),
  );
}

function renderSelectionBar(state: State): void {
  if (!els) return;
  const range = state.selection;
  els.loop.classList.toggle("is-active", state.loop && Boolean(range));
  els.loop.disabled = !range;
  els.loop.setAttribute("aria-pressed", state.loop && range ? "true" : "false");
  if (!range) {
    mount(els.selectionBar, h("p", { class: "hint" }, "파형을 끌어서 고치거나 반복해 들을 구간을 고르세요. 클릭하면 그 위치로 옮겨 가요."));
    return;
  }
  mount(
    els.selectionBar,
    h("span", { class: "selection-label" }, h("b", { class: "mono" }, rangeLabel(range, true)), h("span", { class: "muted" }, ` · ${(range.endSeconds - range.startSeconds).toFixed(1)}초 선택`)),
    h("div", { class: "selection-actions" },
      els.loop,
      h("button", { type: "button", class: "button primary small", onClick: () => { set({ scope: "range" }); focusFeedback(); } }, icon("wand", 14), "이 구간 고치기"),
      h("button", { type: "button", class: "button ghost small", onClick: () => clearSelection() }, icon("close", 14), "선택 해제"),
    ),
  );
}

function previousFull(song: Song, version: Version): Version | null {
  const fulls = song.versions.filter((item) => !isAutomaticAttempt(item) && item.kind === "full");
  const index = fulls.findIndex((item) => item.id === version.id);
  return index > 0 ? fulls[index - 1] : null;
}

function renderInputs(state: State, song: Song): void {
  if (!els) return;
  const current = node(state.activeVersionId);
  els.inputs.hidden = !current;
  if (!current) {
    els.inputs.replaceChildren();
    return;
  }
  const version = current.version;
  const parent = node(versionSourceParent(song, version)?.id);
  const baselineVersion = parent?.version ?? previousFull(song, version);
  const baselineName = parent?.short ?? (baselineVersion ? node(baselineVersion.id)?.short : null);
  const diff = baselineVersion ? tagDiff(baselineVersion.stylePrompt, version.stylePrompt) : [];
  const feedback = song.feedback.find((item) => item.feedbackId === version.feedbackId);
  const strength = strengthFromValue(version.repaintStrength);
  const production = version.productionRules;
  mount(
    els.inputs,
    h(
      "div",
      { class: "card-head" },
      h("h3", null, "이 버전을 만든 입력"),
      h(
        "button",
        {
          type: "button",
          class: "button ghost small",
          onClick: async () => {
            await navigator.clipboard.writeText(version.stylePrompt);
            toast("스타일 태그를 복사했어요.");
          },
        },
        icon("copy", 14),
        "스타일 복사",
      ),
    ),
    feedback &&
      h(
        "div",
        { class: "request" },
        h("span", { class: "label" }, "내 요청"),
        h("p", { class: "request-text" }, `“${feedback.text || "요청 없이 다시 만들기"}”`),
        feedback.plan && h("p", { class: "request-plan" }, feedback.plan.summary),
      ),
    baselineVersion &&
      (diff.length
        ? h(
            "div",
            { class: "diff" },
            h("span", { class: "label" }, `${baselineName}에서 바뀐 태그`),
            h("div", { class: "chips" }, diff.map((change) => h("span", { class: `tag-chip op-${change.op}` }, change.op === "add" ? "+ " : "− ", change.term))),
          )
        : h("p", { class: "hint" }, version.editRange ? `${baselineName}과 같은 스타일로 구간만 다시 만들었어요.` : version.quality?.processing ? "처음 생성한 음원을 보존하고 별도 재생본을 정리했어요." : `${baselineName}과 스타일이 같아요.`)),
    h("p", { class: "caption-text mono" }, version.stylePrompt),
    production && (production.preset || production.rules.length > 0) && h("details", { class: "disclosure" },
      h("summary", null, "이 버전에 보낸 제작 규칙"),
      production.preset && h("p", { class: "hint" }, production.preset.label),
      h("ul", { class: "production-selected" }, production.rules.filter((rule) => production.appliedRuleIds.includes(rule.id)).map((rule) => h("li", null, rule.label))),
      production.skippedRuleIds.includes("clear-vocal") && h("p", { class: "hint" }, "연주곡이라 보컬 규칙은 보내지 않았어요."),
      production.skippedRuleIds.includes("phrase-breathing") && h("p", { class: "hint" }, "연주곡이라 가사 호흡 규칙은 보내지 않았어요."),
      h("p", { class: "hint" }, "만들 당시 선택과 문장을 저장한 기록이에요."),
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
    version.instruction &&
      h(
        "div",
        { class: "notice tone-warn" },
        h("p", null, h("b", null, "예전 방식으로 만든 버전이에요. "), "자유 문장이 엔진의 작업 지시 자리를 덮어써서, 요청이 제대로 반영되지 않았을 수 있어요. 지금은 요청을 스타일 태그와 변화 정도로 전해요."),
        h("p", { class: "mono legacy-instruction" }, version.instruction),
      ),
    h("details", { class: "disclosure" }, h("summary", null, "가사 보기"), h("pre", { class: "lyrics-view" }, version.lyrics)),
    version.lyricsOriginal !== undefined && version.lyricsOriginal !== version.lyrics && h("details", { class: "disclosure" }, h("summary", null, "제작 안내를 넣기 전 가사 원문"), h("pre", { class: "lyrics-view" }, version.lyricsOriginal)),
    version.coverSource && h("p", { class: "hint" }, `이 버전은 원본 음원을 참조해 만든 커버예요. 원본 따르기 ${Math.round((version.coverStrength ?? 0.7) * 100)}% · 원본 파일 크기와 SHA-256을 확인한 기록이 있어요.`),
    version.songPlan && h("details", { class: "disclosure" }, h("summary", null, "만들 때 보낸 전개·호흡 안내"), songPlanView(version.songPlan)),
  );
}

// ---------------------------------------------------------------------------
// Inspector
// ---------------------------------------------------------------------------

function renderTabs(state: State): void {
  if (!els) return;
  const version = activeVersion();
  const warnings = version?.findings.filter((item) => item.severity !== "info").length ?? 0;
  const tabs: Array<[InspectorTab, string, Child]> = [
    ["fix", "고치기", null],
    ["review", "평가", version && version.review.status === "unreviewed" ? h("i", { class: "tab-dot", "aria-label": "아직 안 들음" }) : null],
    ["details", "검사", warnings ? h("em", { class: "tab-count" }, String(warnings)) : null],
  ];
  mount(
    els.tabs,
    tabs.map(([key, label, extra]) =>
      h("button", { type: "button", role: "tab", class: state.tab === key ? "is-active" : "", "aria-selected": state.tab === key ? "true" : "false", "aria-controls": `panel-${key}`, onClick: () => set({ tab: key }) }, label, extra),
    ),
  );
  for (const [key, panel] of Object.entries(els.panels)) panel.hidden = key !== state.tab;
}

function renderFix(state: State, song: Song): void {
  if (!els) return;
  const version = activeVersion();
  const range = state.selection;
  const scope = range ? state.scope : "song";
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
      }, range ? `구간 ${rangeLabel(range)}` : "구간 (선택 없음)"),
    ),
    h("p", { class: "hint" }, scope === "range"
      ? "그 구간만 다시 만들고 나머지는 최대한 그대로 둬요. 경계가 어색하면 구간을 조금 넓혀 보세요."
      : "요청에 맞춰 곡 전체를 새로 만들어요. 가사와 편곡을 이어 받으며, 선율은 달라질 수 있어요. 지금 버전은 그대로 남아요."),
  );

  const rules = state.rules.filter((rule) => QUICK_RULES.includes(rule.key)).sort((a, b) => QUICK_RULES.indexOf(a.key) - QUICK_RULES.indexOf(b.key));
  const visible = showAllChips ? rules : rules.slice(0, 7);
  mount(
    els.fixChips,
    h("span", { class: "label" }, "자주 하는 요청"),
    h(
      "div",
      { class: "chips" },
      visible.map((rule) =>
        h(
          "button",
          {
            type: "button",
            class: "chip small",
            "data-tip": `“${rule.example}”을 적어 넣어요`,
            onClick: () => {
              if (!els) return;
              const current = els.fixArea.value.trim();
              els.fixArea.value = current ? `${current}, ${rule.example}` : rule.example;
              set({ feedback: els.fixArea.value });
              els.fixArea.focus();
            },
          },
          rule.label,
        ),
      ),
      rules.length > 7 &&
        h(
          "button",
          {
            type: "button",
            class: "chip small ghost",
            onClick: () => {
              showAllChips = !showAllChips;
              renderFix(get(), song);
            },
          },
          showAllChips ? "접기" : `+${rules.length - 7}`,
        ),
    ),
  );
  if (els.fixArea.value !== state.feedback) els.fixArea.value = state.feedback;

  mount(els.fixOptions, scope === "range"
    ? h("div", { class: "field" }, h("span", { class: "label" }, "얼마나 바꿀까요?"),
      h("div", { class: "segmented block" }, (["light", "medium", "strong"] as Strength[]).map((key) =>
        h("button", { type: "button", class: state.strength === key ? "is-active" : "", "aria-pressed": state.strength === key ? "true" : "false", "data-tip": strengthCopy[key].hint, onClick: () => set({ strength: key, plan: null }) }, strengthCopy[key].label))))
    : h("div", { class: "field" }, h("span", { class: "label" }, "새로 만들 버전"),
      h("div", { class: "segmented block" }, [1, 2, 3, 4].map((count) =>
        h("button", { type: "button", class: state.planVersions === count ? "is-active" : "", "aria-pressed": state.planVersions === count ? "true" : "false", onClick: () => set({ planVersions: count, plan: null }) }, `${count}개`))),
    ));


  const assistant = state.settings.assistant;
  els.assistantLabel.textContent =
    assistant.kind === "rules" ? "도우미: 기본 규칙" : `도우미: 로컬 LLM · ${assistant.model || "모델 미선택"}`;
  els.assistantLabel.dataset.tip = "설정에서 도우미를 바꿔요";
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
  if (!feedback) {
    toast("무엇이 마음에 안 드는지 한 줄 적어 주세요. 위의 칩을 눌러도 돼요.", { tone: "error" });
    els.fixArea.focus();
    return;
  }
  const ticket = planTicket();
  if (!ticket) return;
  const range = state.scope === "range" && state.selection ? { ...state.selection } : null;
  set({ planning: true, plan: null });
  const label = state.settings.assistant.kind === "rules" ? "해석하는 중" : "LLM이 해석하는 중";
  const plan = await withBusy(els.planButton, label, () =>
    api.plan({ songId: ticket.song.songId, versionId: version.id, feedback, range, strength: state.strength, versions: state.planVersions }),
  );
  const fresh = isPlanTicket(ticket);
  set({ planning: false, ...(fresh ? { plan: plan ?? null } : {}) });
  if (fresh && plan) els?.planBox.scrollIntoView({ block: "start", behavior: "smooth" });
}

function planActionText(plan: Plan): string {
  if (plan.action === "repaint" && plan.range) {
    return `구간 다시 만들기 · ${rangeLabel(plan.range)} · 변화 ${strengthCopy[plan.strength].label}`;
  }
  return `곡 전체 새로 만들기 · 버전 ${plan.versions}개`;
}

function renderPlan(state: State, song: Song): void {
  if (!els) return;
  const plan = state.plan;
  if (!plan) {
    els.planBox.replaceChildren();
    return;
  }
  const context = planTicket();
  if (plan.candidateId !== state.activeVersionId || !context) {
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
      h("div", { class: "plan-action" }, icon(plan.action === "repaint" ? "branch" : "refresh", 16), h("span", null, planActionText(plan))),
      plan.changes.length
        ? h(
            "div",
            { class: "plan-changes" },
            h("span", { class: "label" }, "바꿀 스타일 태그 ", h("em", null, "눌러서 빼거나 되살려요")),
            h(
              "div",
              { class: "chips" },
              plan.changes.map((change) => {
                const applied = change.op === "add" ? hasTag(plan.stylePrompt, change.term) : !hasTag(plan.stylePrompt, change.term);
                return h(
                  "button",
                  {
                    type: "button",
                    class: `tag-chip op-${change.op}${applied ? "" : " is-reverted"}`,
                    "aria-pressed": applied ? "true" : "false",
                    "data-tip": applied ? (change.op === "add" ? "이 태그를 빼요" : "이 태그를 되살려요") : "다시 적용해요",
                    onClick: () => updatePlan({ stylePrompt: toggleTag(plan.stylePrompt, change.term) }),
                  },
                  change.op === "add" ? "+ " : "− ",
                  change.term,
                  change.label && h("small", null, change.label),
                );
              }),
            ),
          )
        : null,
      plan.notes.map((note) => h("p", { class: "plan-note" }, icon("info", 14), note)),
      plan.bpm ? h("p", { class: "plan-note" }, icon("info", 14), `빠르기도 ${plan.bpm} BPM으로 바꿔요.`) : null,
      plan.lyrics && h("details", { class: "disclosure" }, h("summary", null, "바뀐 가사 보기"), h("pre", { class: "lyrics-view" }, plan.lyrics)),
      h("details", { class: "disclosure" }, h("summary", null, "엔진에 보낼 스타일 직접 고치기"), caption),
      plan.action === "repaint"
        ? h("p", { class: "hint" }, "구간 밖도 조금 달라질 수 있어요. 만든 뒤 ‘비교’로 원본과 같은 위치를 번갈아 들어 보세요.")
        : h("p", { class: "hint" }, "가사와 편곡으로 새 전체 곡을 만들어요. 선율과 목소리는 달라질 수 있으며, 지금까지 만든 버전은 그대로 남아요."),
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
  void song;
}

function renderHistory(song: Song): void {
  if (!els) return;
  const records = [...song.feedback].reverse();
  if (!records.length) {
    mount(els.history, h("h3", { class: "panel-subtitle" }, "고친 기록"), h("p", { class: "hint" }, "요청할 때마다 여기에 쌓여요. 어떤 말이 어떤 버전이 됐는지 따라갈 수 있어요."));
    return;
  }
  const running = get().task?.songId === song.songId;
  mount(
    els.history,
    h("h3", { class: "panel-subtitle" }, "고친 기록"),
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
          h("p", { class: "history-meta" }, [source?.short, record.range ? rangeLabel(record.range) : "곡 전체", relativeTime(record.createdAt)].filter(Boolean).join(" · ")),
          h(
            "div",
            { class: "history-results" },
            results.map((item) =>
              h("button", { type: "button", class: "link", onClick: () => selectVersion(item.id) }, "→ ", node(item.id)?.short ?? "새 버전"),
            ),
            status,
          ),
        );
      }),
    ),
  );
}

function renderReview(state: State): void {
  if (!els) return;
  const version = activeVersion();
  if (!version) {
    els.review.replaceChildren();
    return;
  }
  const review = version.review;
  const songId = state.song.song!.songId;
  const save = async (status: ReviewStatus, rating: number | null, note?: string) => {
    try {
      const ticket = songTicket(songId);
      if (!ticket) return false;
      if (!applySongForContext(await api.review({ songId, versionId: version.id, status, rating, note }), ticket)) return false;
      if (songBusy()) toast("만드는 동안에도 평가와 메모를 저장했어요.", { tone: "ok" });
      return true;
    } catch (error) {
      toast(errorText(error), { tone: "error" });
      return false;
    }
  };
  const verdicts: Array<[ReviewStatus, string, string]> = [
    ["approved", "좋아요", "thumbUp"],
    ["listened", "들어 봤어요", "check"],
    ["rejected", "별로예요", "thumbDown"],
  ];
  mount(
    els.review,
    h("h2", { class: "panel-title" }, `${node(version.id)?.short ?? "이 버전"} · 직접 들어 본 판단`),
    h("p", { class: "hint" }, "‘들어 봤어요’는 청취 기록이에요. 마음에 드는 정도는 좋아요·별로예요와 별점으로 남겨요."),
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
            onClick: () => void save(review.status === status ? "unreviewed" : status, review.status === status ? null : review.rating),
          },
          icon(iconName, 20),
          label,
        ),
      ),
    ),
    h(
      "div",
      { class: "stars", role: "radiogroup", "aria-label": "별점" },
      h("span", { class: "label" }, "별점 ", h("em", null, "선택")),
      [1, 2, 3, 4, 5].map((value) =>
        h(
          "button",
          {
            type: "button",
            role: "radio",
            "aria-checked": review.rating === value ? "true" : "false",
            "aria-label": `${value}점`,
            class: `star${review.rating && value <= review.rating ? " is-on" : ""}`,
            onClick: () => void save(review.status === "unreviewed" ? "listened" : review.status, value),
          },
          icon("star", 18),
        ),
      ),
    ),
    h("div", { class: "field" }, h("span", { class: "label" }, "메모"), els.noteArea),
    h(
      "button",
      {
        type: "button",
        class: "button secondary",
        onClick: async () => {
          const note = els?.noteArea.value.trim();
          if (!note) return;
          const saved = await save(review.status, review.rating, note);
          if (saved && els && get().activeVersionId === version.id && els.noteArea.value.trim() === note) els.noteArea.value = "";
        },
      },
      "메모 남기기",
    ),
    review.notes.length
      ? h(
          "ol",
          { class: "notes" },
          [...review.notes].reverse().map((note) => h("li", null, h("time", null, relativeTime(note.createdAt)), h("p", null, note.text))),
        )
      : null,
    h("p", { class: "footnote" }, "자동 검사와 따로 저장돼요. 검사를 통과해도 직접 듣기 전에는 ‘아직 안 들음’으로 남아요."),
  );
  void state;
}

function qualityReport(version: Version): Child {
  const quality = version.quality;
  if (!quality) return null;
  const badge = qualityLabel(quality)!;
  if (quality.complete === false) return h("div", { class: "quality-report" },
    h("span", { class: "pill" }, badge.label), h("p", null, quality.summary),
    h("p", { class: "footnote" }, "소리와 가사를 확인한 뒤 필요한 재시도와 재생본 정리를 이어서 진행해요."));
  const coverage = quality.lyrics.orderedCoverage;
  const knownLyrics = quality.lyrics.status === "pass" || quality.lyrics.status === "warning";
  const processing = processingText(quality.processing);
  const retries = [...new Set(quality.retryReasons.map(qualityRetryReason))];
  const preparation = quality.preparation;
  const extended = preparation && typeof preparation.requestedDurationSeconds === "number" && typeof preparation.effectiveDurationSeconds === "number"
    && preparation.effectiveDurationSeconds > preparation.requestedDurationSeconds;
  return h("div", { class: "quality-report" },
    h("div", { class: "row" }, h("span", { class: `pill tone-${badge.tone}` }, badge.label), version.recommended && h("span", { class: "pill tone-accent" }, "자동 추천")),
    h("p", null, quality.summary),
    extended && h("p", { class: "quality-check" }, `가사 원문을 담도록 길이를 ${lengthLabel(preparation.requestedDurationSeconds)}에서 ${lengthLabel(preparation.effectiveDurationSeconds)}로 늘렸어요.`),
    h("p", { class: "footnote" }, `선택된 결과: ${quality.attempt}번째 시도 · 버전당 처음 생성과 재시도를 합쳐 최대 ${quality.maxAttempts}회`),
    h("div", { class: "quality-check" }, h("h3", { class: "panel-subtitle" }, "소리"), h("p", null, audioQualityText(quality))),
    h("div", { class: "quality-check" }, h("h3", { class: "panel-subtitle" }, "가사"), h("p", null, lyricQualityText(quality)),
      knownLyrics && typeof coverage === "number" && Number.isFinite(coverage) && h("p", { class: "footnote" }, `작성한 순서대로 인식된 가사 ${(Math.max(0, Math.min(1, coverage)) * 100).toFixed(0)}%`)),
    processing && h("div", { class: "quality-check" }, h("h3", { class: "panel-subtitle" }, "재생본 정리"), h("p", null, processing)),
    retries.length ? h("details", { class: "disclosure" }, h("summary", null, "자동 재시도 이유"), h("ul", { class: "quality-reasons" }, retries.map((reason) => h("li", null, reason)))) : null,
    h("p", { class: "footnote" }, "자동 추천은 소리·가사 검사 결과로 골라요. 선율과 감정의 매력은 직접 비교해 평가해 주세요."));
}

function renderDetails(song: Song): void {
  if (!els) return;
  const version = activeVersion();
  if (!version) {
    els.details.replaceChildren();
    return;
  }
  const exports = song.exports.filter((item) => item.candidateId === version.id);
  mount(
    els.details,
    h("h2", { class: "panel-title" }, "자동 검사"),
    qualityReport(version),
    h(
      "div",
      { class: `file-state ${version.fileOk ? "tone-ok" : "tone-danger"}` },
      icon(version.fileOk ? "check" : "close", 16),
      h("div", null, h("b", null, version.fileOk ? "음원 파일 확인됨" : "파일을 쓸 수 없어요"), h("p", null, version.fileOk ? "크기와 SHA-256이 만들 때 기록과 같아요." : version.fileMessage)),
    ),
    h("button", { type: "button", class: "button secondary block", disabled: !version.fileOk, onClick: () => reveal({ kind: "version", versionId: version.id }) }, icon("folder", 16), "Finder에서 음원 파일 보기"),
    h(
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
      ? h(
          "div",
          { class: "exports" },
          h("h3", { class: "panel-subtitle" }, "내보낸 파일"),
          exports.map((item) =>
            h(
              "div",
              { class: "export-row" },
              h("span", null, relativeTime(item.createdAt), item.externalPath ? ` · ${item.externalPath.split("/").pop()}` : ""),
              h("button", { type: "button", class: "button ghost small", disabled: !(item.externalExists || item.exists), onClick: () => reveal({ kind: "export", artifactId: item.artifactId }) }, icon("folder", 14), "보기"),
            ),
          ),
        )
      : null,
    h("p", { class: "footnote" }, version.quality ? "자동 검사는 소리의 측정값과 인식한 가사를 기준으로 해요. 노래의 표현과 음악성은 직접 들어 보세요." : "자동 검사는 파일이 온전한지와 음량·무음·길이만 봐요. 발음과 음악성은 직접 들어야 알 수 있어요."),
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
  const rules = version.productionRules;
  const button = h("button", { type: "button", class: "button primary", disabled: !engineReady(), onClick: (event: Event) => void withBusy(event.currentTarget as HTMLButtonElement, "새 버전을 준비하는 중", async () => {
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
    h("p", { class: "dialog-text" }, `「${sourceName}」의 가사와 편곡으로 새 전체 곡을 만들어요. 원본 음원을 참조하지 않아 선율과 목소리는 달라질 수 있어요. 원본은 그대로 남아요.`),
    h("p", { class: "hint" }, rules?.preset ? `${rules.preset.label} · 만들 당시 제작 규칙을 이어 받아요.` : "만들 당시 스타일과 제작 규칙을 이어 받아요."),
    h("label", { class: "field" }, h("span", { class: "label" }, "새 스타일"), style),
    h("label", { class: "field" }, h("span", { class: "label" }, "가사"), lyrics),
    h("label", { class: "field" }, h("span", { class: "label" }, "만들 버전"), versions),
    h("div", { class: "dialog-actions cover-actions" }, h("button", { type: "submit", value: "cancel", class: "button ghost" }, "취소"), button),
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
      h("h2", { class: "dialog-title" }, "곡 설정"),
      h("p", { class: "dialog-text" }, "바꾼 설정은 다음에 만드는 버전부터 적용돼요. 이미 만든 버전과 기록은 그대로 남아요."),
      h("label", { class: "field" }, h("span", { class: "label" }, "제목"), title),
      h("label", { class: "field" }, h("span", { class: "label" }, "스타일 태그"), style),
      h("label", { class: "field" }, h("span", { class: "label" }, "가사"), lyrics),
      h("div", { class: "field-row" }, h("label", { class: "field" }, h("span", { class: "label" }, "길이 (초)"), duration), h("label", { class: "field" }, h("span", { class: "label" }, "빠르기 (BPM)"), bpm)),
      h(
        "div",
        { class: "dialog-actions" },
        h("button", { type: "submit", value: "cancel", class: "button ghost" }, "취소"),
        h("button", { type: "submit", value: "save", class: "button secondary" }, "저장"),
        h("button", { type: "submit", value: "generate", class: "button primary", disabled: !engineReady() }, `저장하고 버전 ${get().settings.defaultVersions}개 만들기`),
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
// in the inspector never loses focus mid-generation.
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
  if (all || changed.has("engine") || taskMoved) {
    renderHead(song);
    renderRail(state, song);
  } else if (versionChanged) {
    renderRail(state, song);
  }
  if (all || changed.has("task") || changed.has("engine")) renderBanner(state, song);
  if (versionChanged || taskMoved) renderPlayer(state, song);
  if (changed.has("selection") || changed.has("loop")) {
    if (player) {
      player.loop = state.loop;
      player.setSelection(state.selection);
    }
    renderSelectionBar(state);
  }
  if (versionChanged) renderInputs(state, song);
  if (versionChanged || changed.has("tab")) renderTabs(state);
  const fixKeys: Array<keyof State> = ["selection", "scope", "strength", "planVersions", "feedback", "plan", "planning", "settings", "rules", "engine"];
  if (versionChanged || taskMoved || fixKeys.some((key) => changed.has(key))) renderFix(state, song);
  if (versionChanged) renderReview(state);
  if (versionChanged) renderDetails(song);
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

export function pausePlayback(): void {
  if (player?.playing) player.audio.pause();
}
