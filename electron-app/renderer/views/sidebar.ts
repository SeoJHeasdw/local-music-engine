import { engineReady, go, startEngine } from "../actions.ts";
import { byId, h, icon, mount } from "../dom.ts";
import { elapsed } from "../format.ts";
import { get, type View } from "../store.ts";

const engineCopy: Record<string, { label: string; tone: string }> = {
  checking: { label: "확인 중", tone: "idle" },
  offline: { label: "꺼짐", tone: "off" },
  starting: { label: "켜는 중", tone: "busy" },
  ready: { label: "켜짐", tone: "on" },
  external: { label: "켜짐", tone: "on" },
  stopping: { label: "끄는 중", tone: "busy" },
  failed: { label: "멈춤", tone: "off" },
  missing: { label: "설치 필요", tone: "off" },
};

function navItem(view: View, label: string, iconName: string, options: { sub?: string; badge?: string | null; disabled?: boolean; pulse?: boolean; tip?: string } = {}) {
  const state = get();
  return h(
    "button",
    {
      type: "button",
      class: `nav-item${state.view === view ? " is-active" : ""}`,
      "aria-current": state.view === view ? "page" : undefined,
      disabled: options.disabled,
      "data-tip": options.tip,
      onClick: () => go(view),
    },
    h("span", { class: "nav-icon" }, icon(iconName, 18)),
    h("span", { class: "nav-text" }, h("b", null, label), options.sub && h("small", null, options.sub)),
    options.pulse ? h("i", { class: "nav-pulse", "aria-label": "작업 중" }) : options.badge ? h("em", { class: "nav-count" }, options.badge) : null,
  );
}

export function renderSidebar(): void {
  const state = get();
  const song = state.song.song;
  const engine = state.engine;
  const copy = engineCopy[engine.state] ?? engineCopy.checking;
  const task = state.task;
  const canStart = ["offline", "failed"].includes(engine.state);

  mount(
    byId("sidebar"),
    h(
      "div",
      { class: "brand" },
      h("span", { class: "brand-mark", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"), h("i")),
      h("div", null, h("strong", null, "Music Studio"), h("small", null, "javis · 로컬 작곡")),
    ),
    h(
      "div",
      { class: "nav-group" },
      navItem("library", "내 곡", "library", { badge: state.songs.length ? String(state.songs.length) : null }),
      navItem("create", "새 곡 만들기", "plus"),
      navItem("studio", "작업실", "studio", {
        sub: song ? song.title : "열린 곡 없음",
        disabled: !song,
        pulse: Boolean(task && song && task.songId === song.songId),
        tip: song ? undefined : "곡을 열면 버전을 듣고 고칠 수 있어요",
      }),
      navItem("settings", "설정", "settings"),
    ),
    task &&
      h(
        "button",
        {
          type: "button",
          class: "sidebar-task",
          onClick: () => {
            const current = get();
            if (current.song.song?.songId === task.songId) go("studio");
          },
        },
        h("span", { class: "sidebar-task-head" }, h("b", null, task.songTitle), h("span", { class: "mono" }, elapsed(task.startedAt))),
        h("span", { class: "sidebar-task-stage" }, task.cancelling ? "취소하는 중" : `${task.label} · ${task.done}/${task.total}`),
        h("span", { class: "meter", "aria-hidden": "true" }, h("i", { style: { width: `${Math.round(task.progress * 100)}%` } })),
      ),
    h(
      "div",
      { class: `engine-card tone-${copy.tone}` },
      h(
        "div",
        { class: "engine-row" },
        h("span", { class: "engine-dot", "aria-hidden": "true" }),
        h("span", { class: "engine-name" }, "음악 엔진"),
        h("span", { class: "engine-state" }, copy.label),
      ),
      engine.state === "starting" && h("p", { class: "engine-detail" }, engine.detail, " · ", elapsed(engine.since)),
      ["failed", "missing"].includes(engine.state) && h("p", { class: "engine-detail" }, engine.detail),
      engineReady() && engine.models.dit && h("p", { class: "engine-detail mono" }, engine.models.dit),
      canStart &&
        h(
          "button",
          { type: "button", class: "button small primary block", onClick: () => void startEngine() },
          icon("power", 14),
          "엔진 켜기",
        ),
    ),
  );
}
