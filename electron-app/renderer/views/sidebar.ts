import type { SongSummary } from "../../shared.ts";
import { engineReady, go, navigateHistory, openSong, startEngine } from "../actions.ts";
import { byId, h, icon, mount } from "../dom.ts";
import { elapsed, modelLabel, relativeTime } from "../format.ts";
import { canGoBack, canGoForward } from "../history.ts";
import { get, type View } from "../store.ts";

const engineCopy: Record<string, { label: string; tone: string }> = {
  checking: { label: "확인 중", tone: "idle" },
  offline: { label: "꺼져 있음", tone: "off" },
  starting: { label: "켜는 중", tone: "busy" },
  ready: { label: "켜짐", tone: "on" },
  external: { label: "켜짐", tone: "on" },
  stopping: { label: "끄는 중", tone: "busy" },
  failed: { label: "멈춤", tone: "off" },
  missing: { label: "설치 필요", tone: "off" },
};

const RECENT_LIMIT = 12;

function navItem(view: View, label: string, iconName: string, badge?: string | null) {
  const active = get().view === view;
  return h(
    "button",
    { type: "button", class: `nav-item${active ? " is-active" : ""}`, "aria-current": active ? "page" : undefined, onClick: () => go(view) },
    h("span", { class: "nav-icon" }, icon(iconName, 17)),
    h("span", { class: "nav-label" }, label),
    badge && h("em", { class: "nav-count" }, badge),
  );
}

function songRow(song: SongSummary) {
  const state = get();
  const active = state.view === "studio" && state.song.song?.songId === song.songId;
  const running = song.running || state.task?.songId === song.songId;
  return h(
    "button",
    {
      type: "button",
      class: `song-link${active ? " is-active" : ""}${song.error ? " is-broken" : ""}`,
      "aria-current": active ? "page" : undefined,
      "data-tip": song.error ?? song.title,
      onClick: () => void openSong(song.songId),
    },
    h("span", { class: "song-link-mark", "aria-hidden": "true" }, running ? h("i", { class: "pulse-dot" }) : icon("note", 14)),
    h("span", { class: "song-link-title" }, song.title),
    h("span", { class: "song-link-meta" }, running ? "만드는 중" : relativeTime(song.updatedAt)),
  );
}

export function renderSidebar(): void {
  const state = get();
  const engine = state.engine;
  const copy = engineCopy[engine.state] ?? engineCopy.checking;
  const task = state.task;
  const canStart = ["offline", "failed"].includes(engine.state);
  const recent = [...state.songs]
    .sort((a, b) => Date.parse(b.updatedAt ?? "") - Date.parse(a.updatedAt ?? "") || 0)
    .slice(0, RECENT_LIMIT);

  mount(
    byId("sidebar-body"),
    h(
      "div",
      { class: "brand" },
      h("span", { class: "brand-mark", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"), h("i")),
      h("strong", null, "Music Studio"),
    ),
    h(
      "button",
      { type: "button", class: `new-song${state.view === "create" ? " is-active" : ""}`, "data-tip": "새 곡 만들기 (⌘N)", onClick: () => go("create") },
      icon("plus", 16),
      "새 곡 만들기",
    ),
    h("div", { class: "nav-group" }, navItem("library", "내 곡", "library", state.songs.length ? String(state.songs.length) : null)),
    h(
      "div",
      { class: "song-links" },
      h("p", { class: "sidebar-label" }, "최근 곡"),
      recent.length ? recent.map(songRow) : h("p", { class: "sidebar-empty" }, "아직 만든 곡이 없어요"),
      state.songs.length > RECENT_LIMIT && h("button", { type: "button", class: "song-links-more", onClick: () => go("library") }, `전체 ${state.songs.length}곡 보기`),
    ),
    task &&
      h(
        "button",
        {
          type: "button",
          class: "sidebar-task",
          onClick: () => void openSong(task.songId),
        },
        h("span", { class: "sidebar-task-head" }, h("b", null, task.songTitle), h("span", { class: "mono" }, elapsed(task.startedAt))),
        h("span", { class: "sidebar-task-stage" }, task.cancelling ? "취소하는 중" : `${task.label}${task.total > 1 ? ` · ${task.done}/${task.total}` : ""}`),
        h("span", { class: "meter", "aria-hidden": "true" }, h("i", { style: { width: `${Math.round(task.progress * 100)}%` } })),
      ),
    // 설정은 음악 엔진 카드에서 연다. 엔진 상태와 모델·도우미·저장 위치를 한곳에서 본다.
    h(
      "div",
      { class: `engine-card tone-${copy.tone}${state.view === "settings" ? " is-active" : ""}` },
      h(
        "button",
        { type: "button", class: "engine-open", "data-tip": "음악 엔진과 앱 설정 (⌘,)", onClick: () => go("settings") },
        h("span", { class: "engine-dot", "aria-hidden": "true" }),
        h(
          "span",
          { class: "engine-text" },
          h("b", null, "음악 엔진 · ", h("span", { class: "engine-state" }, copy.label)),
          h(
            "small",
            null,
            engine.state === "starting"
              ? `${engine.detail} · ${elapsed(engine.since)}`
              : engineReady() && engine.models.music
                ? modelLabel(engine.models.music)
                : ["failed", "missing"].includes(engine.state)
                  ? engine.detail
                  : "설정",
          ),
        ),
        h("span", { class: "engine-gear" }, icon("settings", 16)),
      ),
      canStart &&
        h(
          "button",
          { type: "button", class: "button small primary block", onClick: () => void startEngine() },
          icon("power", 14),
          engine.owned && engine.state === "failed" ? "다시 켜기" : "엔진 켜기",
        ),
    ),
  );
}

const viewTitles: Record<View, string> = { library: "내 곡", create: "새 곡 만들기", studio: "작업 중인 곡", settings: "설정" };

// The strip above every screen: where you are, and the running task from any screen.
export function renderTopbar(): void {
  const state = get();
  const song = state.song.song;
  const task = state.task;
  const crumbs =
    state.view === "studio" && song
      ? [h("button", { type: "button", class: "crumb", onClick: () => go("library") }, "내 곡"), h("span", { class: "crumb-sep", "aria-hidden": "true" }, icon("forward", 13)), h("span", { class: "crumb is-current" }, song.title)]
      : [h("span", { class: "crumb is-current" }, viewTitles[state.view])];
  mount(
    byId("topbar"),
    h("nav", { class: "crumbs", "aria-label": "현재 위치" }, crumbs),
    task &&
      !(state.view === "studio" && song?.songId === task.songId) &&
      h(
        "button",
        { type: "button", class: "topbar-task", onClick: () => void openSong(task.songId), "data-tip": "만드는 곡으로 가기" },
        h("span", { class: "task-spinner small", "aria-hidden": "true" }),
        h("span", null, `「${task.songTitle}」 ${task.cancelling ? "취소하는 중" : task.label}`),
        h("span", { class: "mono muted" }, `${Math.round(task.progress * 100)}%`),
      ),
  );
}

export function renderWindowControls(onToggleSidebar: () => void, sidebarClosed: boolean): void {
  const control = (iconName: string, label: string, tip: string, onClick: () => void, disabled = false) =>
    h("button", { type: "button", class: "window-control", "aria-label": label, "data-tip": tip, disabled, onClick }, icon(iconName, iconName === "sidebar" ? 17 : 18));
  mount(
    byId("window-controls"),
    control("sidebar", sidebarClosed ? "사이드바 열기" : "사이드바 접기", sidebarClosed ? "사이드바 열기 (⌘\\)" : "사이드바 접기 (⌘\\)", onToggleSidebar),
    control("back", "뒤로", "뒤로 (⌘[)", () => void navigateHistory(-1), !canGoBack()),
    control("forward", "앞으로", "앞으로 (⌘])", () => void navigateHistory(1), !canGoForward()),
  );
}
