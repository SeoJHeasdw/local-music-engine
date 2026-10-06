import { app, BrowserWindow, dialog, protocol, screen } from "electron";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { ARTIFACT_SCHEME, handleArtifactRequest } from "./main/audio.ts";
import { Controller } from "./main/ipc.ts";
import { distRoot } from "./main/paths.ts";
import { loadAppState, loadSettings, updateAppState } from "./main/settings.ts";
import { MIN_HEIGHT, MIN_WIDTH, windowBounds, type WindowState } from "./main/window-state.ts";

protocol.registerSchemesAsPrivileged([
  {
    scheme: ARTIFACT_SCHEME,
    privileges: { secure: true, standard: true, stream: true, supportFetchAPI: true },
  },
]);

// Tests point the app at a throwaway data folder so real settings and history stay untouched.
if (process.env.MUSIC_STUDIO_USER_DATA) app.setPath("userData", process.env.MUSIC_STUDIO_USER_DATA);

// Screenshot mode renders one view, captures it and quits; it never starts the engine.
const screenshotPath = process.env.MUSIC_STUDIO_SCREENSHOT;
// e.g. MUSIC_STUDIO_QUERY="view=studio&tab=review"
const screenshotQuery = Object.fromEntries(new URLSearchParams(process.env.MUSIC_STUDIO_QUERY ?? ""));

let mainWindow: BrowserWindow | null = null;
let quitting = false;
let shutdownStarted = false;
const controller = new Controller(() => mainWindow);

function shutdown(): void {
  if (shutdownStarted) return;
  shutdownStarted = true;
  void controller.tasks.shutdown().finally(() => {
    controller.engine.disposeOwned();
    quitting = true;
    // Cleanup is done, so leave directly. A second app.quit() is not enough: when the
    // first quit came from SIGTERM/SIGINT (Ctrl-C in app.sh), Electron forgets it was
    // quitting after before-quit cancelled it, closes the window and keeps running.
    app.exit(0);
  });
}

function createWindow(saved: WindowState | null): void {
  // Screenshots keep a fixed size; the app reopens where and how it was left.
  const bounds = screenshotPath
    ? { width: 1440, height: 900 }
    : windowBounds(saved?.bounds ?? null, screen.getAllDisplays().map((display) => display.workArea), screen.getPrimaryDisplay().workArea);
  mainWindow = new BrowserWindow({
    ...bounds,
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    titleBarStyle: "hiddenInset",
    // Vertically centred with the sidebar's back/forward controls in the 52px title row.
    trafficLightPosition: { x: 18, y: 20 },
    backgroundColor: "#0f1110",
    title: "Music Studio",
    show: false,
    webPreferences: {
      preload: path.join(distRoot, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  const window = mainWindow;
  void window.loadFile(path.join(distRoot, "index.html"), {
    query: screenshotQuery,
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event, url) => {
    if (url !== window.webContents.getURL()) event.preventDefault();
  });
  window.once("ready-to-show", () => {
    if (!screenshotPath && saved?.maximized && !saved.fullscreen) window.maximize();
    window.show();
    if (!screenshotPath && saved?.fullscreen) window.setFullScreen(true);
  });
  const tellFullscreen = () => {
    if (!window.isDestroyed()) window.webContents.send("music:event", { type: "window", fullscreen: window.isFullScreen() });
  };
  window.on("enter-full-screen", tellFullscreen);
  window.on("leave-full-screen", tellFullscreen);
  window.webContents.on("did-finish-load", tellFullscreen);
  if (!screenshotPath) {
    let timer: NodeJS.Timeout | undefined;
    const remember = () => {
      if (window.isDestroyed()) return;
      void updateAppState({ window: { bounds: window.getNormalBounds(), fullscreen: window.isFullScreen(), maximized: window.isMaximized() } }).catch(() => undefined);
    };
    const soon = () => {
      clearTimeout(timer);
      timer = setTimeout(remember, 400);
    };
    for (const name of ["resize", "move", "maximize", "unmaximize", "enter-full-screen", "leave-full-screen"] as const) window.on(name as "resize", soon);
    window.on("close", () => {
      clearTimeout(timer);
      remember();
    });
  }
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });

  if (screenshotPath) {
    window.webContents.once("did-finish-load", async () => {
      await new Promise((resolve) => setTimeout(resolve, Number(process.env.MUSIC_STUDIO_DELAY ?? 1600)));
      const image = await window.webContents.capturePage();
      await writeFile(screenshotPath, image.toPNG());
      app.quit();
    });
  }
}

// One window owns generation. A second launch focuses the first instead of racing it on
// the same project lock. Screenshot runs are throwaway and skip the lock.
if (!screenshotPath && !app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });
}

// Ctrl-C in the terminal that ran app.sh should clean up like ⌘Q, not orphan the engine.
// On macOS Electron itself turns these signals into a quit (before-quit below); these
// handlers cover platforms where Node receives the signal first.
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    shutdown();
  });
}

app.whenReady().then(async () => {
  protocol.handle(ARTIFACT_SCHEME, handleArtifactRequest);
  const settings = await loadSettings();
  const saved = (await loadAppState()).window;
  controller.register();
  createWindow(saved);
  void controller.engine.check().then((status) => {
    if (!screenshotPath && settings.engineAutoStart && status.state === "offline") void controller.engine.start();
  });
  controller.engine.startMonitoring();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) void loadAppState().then((state) => createWindow(state.window));
  });
});

app.on("before-quit", (event) => {
  if (quitting) return;
  event.preventDefault();
  if (shutdownStarted) return;
  const task = controller.tasks.snapshot();
  if (task && mainWindow) {
    const choice = dialog.showMessageBoxSync(mainWindow, {
      type: "warning",
      buttons: ["취소하고 종료", "계속 만들기"],
      defaultId: 1,
      cancelId: 1,
      message: `「${task.songTitle}」을 만드는 중이에요.`,
      detail: "종료하면 진행 중인 작업을 취소해요. 이미 끝난 버전은 남아 있어요.",
    });
    if (choice === 1) {
      event.preventDefault();
      return;
    }
  }
  shutdown();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
