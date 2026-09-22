import { app, BrowserWindow, dialog, protocol } from "electron";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { ARTIFACT_SCHEME, handleArtifactRequest } from "./main/audio.ts";
import { Controller } from "./main/ipc.ts";
import { distRoot } from "./main/paths.ts";
import { loadSettings } from "./main/settings.ts";

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
    app.quit();
  });
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1080,
    minHeight: 700,
    titleBarStyle: "hiddenInset",
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
  window.once("ready-to-show", () => window.show());
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
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    shutdown();
  });
}

app.whenReady().then(async () => {
  protocol.handle(ARTIFACT_SCHEME, handleArtifactRequest);
  const settings = await loadSettings();
  controller.register();
  createWindow();
  void controller.engine.check().then((status) => {
    if (!screenshotPath && settings.aceAutoStart && status.state === "offline") void controller.engine.start();
  });
  controller.engine.startMonitoring();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
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
