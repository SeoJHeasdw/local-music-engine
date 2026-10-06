import { api, applySongForContext, go, navigateHistory, refreshSong } from "./actions.ts";
import { byId, isTyping } from "./dom.ts";
import { onHistoryChange, resetHistory } from "./history.ts";
import { installResizer } from "./resize.ts";
import { get, initStore, isSongTicket, listeningRevision, set, songTicket, subscribe, type State, type View } from "./store.ts";
import { TaskListeningFocus } from "./quality.ts";
import { errorText, installMenus, installTooltips, toast } from "./ui.ts";
import { demoDraft, renderCreate, submitCreateShortcut } from "./views/create.ts";
import { renderLibrary } from "./views/library.ts";
import { renderSettings } from "./views/settings.ts";
import { renderSidebar, renderTopbar, renderWindowControls } from "./views/sidebar.ts";
import {
  clearSelection,
  demoInstruments,
  demoLyricsEdit,
  demoPlan,
  demoZoom,
  focusFeedback,
  nudge,
  renderStudio,
  stepVersion,
  toggleCompare,
  toggleLoop,
  togglePlay,
  zoomWave,
} from "./views/studio.ts";

const views: View[] = ["library", "create", "studio", "settings"];
const taskFocus = new TaskListeningFocus();

function showView(view: View): void {
  for (const name of views) byId(`view-${name}`).hidden = name !== view;
  const song = get().song.song;
  document.title = view === "studio" && song ? `${song.title} — Music Studio` : "Music Studio";
}

function render(state: State, changed: Set<keyof State>): void {
  const sidebarKeys: Array<keyof State> = ["view", "engine", "task", "song", "songs"];
  if (sidebarKeys.some((key) => changed.has(key))) {
    renderSidebar();
    renderTopbar();
  }
  if (changed.has("view")) showView(state.view);
  if (state.view === "library" && (changed.has("view") || changed.has("songs") || changed.has("settings") || changed.has("song") || changed.has("task"))) renderLibrary();
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

const SIDEBAR_KEY = "music-studio:sidebar";
const SIDEBAR_WIDTH_KEY = "music-studio:sidebar-width";
const SIDEBAR_DEFAULT = 248;

function installShell(): void {
  const shell = byId("shell");
  let closed = false;
  try {
    closed = localStorage.getItem(SIDEBAR_KEY) === "closed";
  } catch {
    // per-viewer convenience only
  }
  const toggleSidebar = () => {
    closed = !closed;
    apply();
    try {
      localStorage.setItem(SIDEBAR_KEY, closed ? "closed" : "open");
    } catch {
      // ignore
    }
  };
  const apply = () => {
    shell.classList.toggle("is-sidebar-closed", closed);
    renderWindowControls(toggleSidebar, closed);
  };
  apply();
  onHistoryChange(() => renderWindowControls(toggleSidebar, closed));
  installResizer(byId("sidebar-resizer"), {
    storageKey: SIDEBAR_WIDTH_KEY,
    min: 200,
    max: () => Math.min(380, Math.round(window.innerWidth * 0.32)),
    fallback: SIDEBAR_DEFAULT,
    apply: (width) => shell.style.setProperty("--sidebar-w", `${width}px`),
  });

  // Mouse side buttons go back and forward like a browser.
  window.addEventListener("mouseup", (event) => {
    if (event.button === 3 || event.button === 4) {
      event.preventDefault();
      void navigateHistory(event.button === 3 ? -1 : 1);
    }
  });

  document.addEventListener("keydown", (event) => {
    const state = get();
    const meta = event.metaKey || event.ctrlKey;
    if (meta && event.key === "\\") {
      event.preventDefault();
      toggleSidebar();
      return;
    }
    if (meta && (event.key === "[" || event.key === "]")) {
      event.preventDefault();
      void navigateHistory(event.key === "[" ? -1 : 1);
      return;
    }
    if (meta && ["1", "2", "3"].includes(event.key)) {
      event.preventDefault();
      go((["library", "create", "studio"] as View[])[Number(event.key) - 1]);
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
    } else if (key === "=" || key === "+") {
      zoomWave(1);
    } else if (key === "-") {
      zoomWave(-1);
    } else if (key === "0") {
      zoomWave(0);
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
  installMenus();
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
    productionCatalog: boot.productionCatalog,
    info: boot.info,
    activeVersionId: song?.finalVersionId ?? song?.recommendedVersionId ?? song?.versions.findLast((version) => version.quality?.preferred !== false)?.id ?? null,
    selection: null,
    listenToParent: false,
    comparisonVersionId: null,
    loop: false,
    tab: (params.get("tab") as State["tab"]) || "prompt",
    feedback: "",
    scope: "song",
    strength: boot.settings.feedbackStrength,
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
      productionRules: { version: 1, presetId: null, ruleIds: [] },
      vocalLanguage: "ko",
    },
  });
  resetHistory({ view: initialView, songId: initialView === "studio" ? song?.songId ?? null : null });
  subscribe(render);
  taskFocus.track(boot.task, listeningRevision());
  const all = new Set(Object.keys(get()) as Array<keyof State>);
  render(get(), all);

  api.onEvent((event) => {
    if (event.type === "engine") set({ engine: event.engine });
    else if (event.type === "task") {
      taskFocus.track(event.task, listeningRevision());
      set({ task: event.task });
    }
    else if (event.type === "songs") set({ songs: event.songs });
    else if (event.type === "window") document.body.classList.toggle("is-fullscreen", event.fullscreen);
    else if (event.type === "song") {
      applySongForContext(event.song, songTicket(event.songId));
    } else if (event.type === "task-finished") {
      const outcome = event.outcome;
      const state = get();
      const here = state.song.song?.songId === outcome.songId;
      const ticket = songTicket(outcome.songId);
      const firstNew = outcome.recommendedVersionId ?? outcome.newVersionIds[0];
      const focus = taskFocus.complete(outcome, state.song.song?.songId, listeningRevision());
      if (here && focus) {
        void refreshSong(outcome.songId, focus);
      }
      toast(`「${outcome.songTitle}」 ${outcome.message}`, {
        tone: outcome.cancelled ? "info" : outcome.ok ? "ok" : "error",
        timeout: 6000,
        action:
          firstNew && here && outcome.kind !== "repaint"
            ? { label: "새 버전 듣기", run: () => { if (isSongTicket(ticket)) set({ activeVersionId: firstNew, listenToParent: false }); } }
            : undefined,
      });
    }
  });

  const demo = params.get("demo");
  if ((demo === "draft" || demo === "lyrics") && get().view === "create") {
    demoDraft(params.get("description") ?? "늦은 밤 도시를 달리는 감성 힙합. 남자 목소리, 비트와 피아노가 끝까지 이어지게",
      demo === "lyrics" ? "[Verse]\n가로등 아래 혼자 걷던 밤\n네 목소리만 귓가에 남아\n\n[Chorus]\n멈추지 마 이 리듬 위에\n우리 둘만의 도시를 그려" : null,
      params.get("run") === "1" || demo === "draft");
  }
  if (demo && get().song.song) {
    // Screenshot fixtures; the packaged app never passes these parameters.
    if (demo === "selection") {
      const version = get().song.song?.versions[0];
      if (version?.durationSeconds) set({ selection: { startSeconds: version.durationSeconds * 0.3, endSeconds: version.durationSeconds * 0.55 }, scope: "range" });
    } else if (demo === "lyrics") {
      demoLyricsEdit();
    } else if (demo === "zoom") {
      demoZoom();
    } else if (demo === "instruments") {
      demoInstruments();
    } else if (demo === "plan" || demo === "edit-plan") {
      void demoPlan(params.get("feedback") ?? "후렴 발음이 뭉개지고 드럼이 너무 세요", demo === "edit-plan");
    }
  }
}

void start().catch((error) => {
  document.body.textContent = `앱을 시작하지 못했어요: ${errorText(error)}`;
});
