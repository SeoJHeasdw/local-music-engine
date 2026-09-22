import { api, applySong, go } from "./actions.ts";
import { byId, icon, isTyping } from "./dom.ts";
import { get, initStore, set, subscribe, type State, type View } from "./store.ts";
import { errorText, installTooltips, toast } from "./ui.ts";
import { renderCreate, submitCreateShortcut } from "./views/create.ts";
import { renderLibrary } from "./views/library.ts";
import { renderSettings } from "./views/settings.ts";
import { renderSidebar } from "./views/sidebar.ts";
import {
  clearSelection,
  demoPlan,
  focusFeedback,
  nudge,
  renderStudio,
  stepVersion,
  toggleCompare,
  toggleLoop,
  togglePlay,
} from "./views/studio.ts";

const views: View[] = ["library", "create", "studio", "settings"];

function showView(view: View): void {
  for (const name of views) byId(`view-${name}`).hidden = name !== view;
  const song = get().song.song;
  document.title = view === "studio" && song ? `${song.title} — Music Studio` : "Music Studio";
}

function render(state: State, changed: Set<keyof State>): void {
  const sidebarKeys: Array<keyof State> = ["view", "engine", "task", "song", "songs"];
  if (sidebarKeys.some((key) => changed.has(key))) renderSidebar();
  if (changed.has("view")) showView(state.view);
  if (state.view === "library" && (changed.has("view") || changed.has("songs") || changed.has("settings") || changed.has("song"))) renderLibrary();
  if (state.view === "create" && (changed.has("view") || changed.has("engine") || changed.has("settings") || changed.has("create"))) {
    // Typing only patches the draft; the form itself is built once.
    if (changed.has("view") || changed.has("engine") || changed.has("settings")) renderCreate();
  }
  if (state.view === "studio") {
    if (state.song.status !== "ready") {
      set({ view: "library" });
      return;
    }
    renderStudio(changed);
  }
  if (state.view === "settings" && (changed.has("view") || changed.has("settings") || changed.has("engine") || changed.has("task"))) renderSettings();
}

function installShell(): void {
  const toggle = byId<HTMLButtonElement>("sidebar-toggle");
  toggle.append(icon("sidebar", 17));
  const shell = byId("shell");
  const apply = (closed: boolean) => {
    shell.classList.toggle("is-sidebar-closed", closed);
    toggle.setAttribute("aria-expanded", closed ? "false" : "true");
    toggle.dataset.tip = closed ? "사이드바 열기 (⌘\\)" : "사이드바 접기 (⌘\\)";
  };
  let closed = false;
  try {
    closed = localStorage.getItem("music-studio:sidebar") === "closed";
  } catch {
    // per-viewer convenience only
  }
  apply(closed);
  toggle.addEventListener("click", () => {
    closed = !closed;
    apply(closed);
    try {
      localStorage.setItem("music-studio:sidebar", closed ? "closed" : "open");
    } catch {
      // ignore
    }
  });

  document.addEventListener("keydown", (event) => {
    const state = get();
    const meta = event.metaKey || event.ctrlKey;
    if (meta && event.key === "\\") {
      event.preventDefault();
      toggle.click();
      return;
    }
    if (meta && ["1", "2", "3", "4"].includes(event.key)) {
      event.preventDefault();
      go(views[Number(event.key) - 1]);
      return;
    }
    if (meta && event.key.toLowerCase() === "n") {
      event.preventDefault();
      go("create");
      return;
    }
    if (meta && event.key === ",") {
      event.preventDefault();
      go("settings");
      return;
    }
    if (meta && event.key === "Enter" && state.view === "create") {
      event.preventDefault();
      submitCreateShortcut();
      return;
    }
    if (document.querySelector("dialog[open]")) return;
    if (state.view !== "studio" || isTyping(event.target) || meta || event.altKey) return;
    const key = event.key;
    if (key === " ") {
      event.preventDefault();
      togglePlay();
    } else if (key === "ArrowLeft" || key === "ArrowRight") {
      event.preventDefault();
      nudge((key === "ArrowLeft" ? -1 : 1) * (event.shiftKey ? 1 : 5));
    } else if (key === "ArrowUp" || key === "ArrowDown") {
      event.preventDefault();
      stepVersion(key === "ArrowUp" ? -1 : 1);
    } else if (key.toLowerCase() === "c") {
      toggleCompare();
    } else if (key.toLowerCase() === "l") {
      toggleLoop();
    } else if (key.toLowerCase() === "f") {
      event.preventDefault();
      focusFeedback();
    } else if (key === "Escape") {
      clearSelection();
    }
  });
}

async function start(): Promise<void> {
  installTooltips();
  installShell();
  const boot = await api.bootstrap();
  const params = new URLSearchParams(location.search);
  const requested = params.get("view") as View | null;
  const initialView: View =
    requested && views.includes(requested) && (requested !== "studio" || boot.song.status === "ready")
      ? requested
      : boot.song.status === "ready"
        ? "studio"
        : "library";
  const song = boot.song.song;
  initStore({
    view: initialView,
    settings: boot.settings,
    engine: boot.engine,
    songs: boot.songs,
    song: boot.song,
    task: boot.task,
    rules: boot.rules,
    info: boot.info,
    activeVersionId: song?.finalVersionId ?? song?.versions.at(-1)?.id ?? null,
    selection: null,
    listenToParent: false,
    loop: false,
    tab: (params.get("tab") as State["tab"]) || "fix",
    feedback: "",
    scope: "song",
    strength: boot.settings.repaintStrength,
    planVersions: boot.settings.defaultVersions,
    plan: null,
    planning: false,
    create: {
      description: "",
      instrumental: false,
      title: "",
      stylePrompt: "",
      lyrics: "",
      durationSeconds: boot.settings.defaultDurationSeconds,
      versions: boot.settings.defaultVersions,
      bpm: "",
      keyScale: "",
      timeSignature: "",
      drafted: false,
    },
  });
  subscribe(render);
  const all = new Set(Object.keys(get()) as Array<keyof State>);
  render(get(), all);

  api.onEvent((event) => {
    if (event.type === "engine") set({ engine: event.engine });
    else if (event.type === "task") set({ task: event.task });
    else if (event.type === "songs") set({ songs: event.songs });
    else if (event.type === "song") {
      if (get().song.song?.songId === event.song.song?.songId) applySong(event.song);
    } else if (event.type === "task-finished") {
      const outcome = event.outcome;
      const state = get();
      const here = state.song.song?.songId === outcome.songId;
      const firstNew = outcome.newVersionIds[0];
      if (here && firstNew && outcome.kind === "repaint") {
        // A finished edit is what the listener was waiting for; open it next to its original.
        void api.refreshSong().then((next) => applySong(next, firstNew));
      }
      toast(`「${outcome.songTitle}」 ${outcome.message}`, {
        tone: outcome.cancelled ? "info" : outcome.ok ? "ok" : "error",
        timeout: 6000,
        action:
          firstNew && here && outcome.kind !== "repaint"
            ? { label: "새 버전 듣기", run: () => set({ activeVersionId: firstNew, listenToParent: false }) }
            : undefined,
      });
    }
  });

  const demo = params.get("demo");
  if (demo && get().song.song) {
    // Screenshot fixtures; the packaged app never passes these parameters.
    if (demo === "selection") {
      const version = get().song.song?.versions[0];
      if (version?.durationSeconds) set({ selection: { startSeconds: version.durationSeconds * 0.3, endSeconds: version.durationSeconds * 0.55 }, scope: "range" });
    } else if (demo === "plan" || demo === "edit-plan") {
      void demoPlan(params.get("feedback") ?? "후렴 발음이 뭉개지고 드럼이 너무 세요", demo === "edit-plan");
    }
  }
}

void start().catch((error) => {
  document.body.textContent = `앱을 시작하지 못했어요: ${errorText(error)}`;
});
