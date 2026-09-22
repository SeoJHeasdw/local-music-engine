import { contextBridge, ipcRenderer } from "electron";
import type { MusicAppApi, MusicEvent } from "./shared.ts";

const invoke = (channel: string, ...args: unknown[]) => ipcRenderer.invoke(`music:${channel}`, ...args);

const api: MusicAppApi = {
  bootstrap: () => invoke("bootstrap"),
  listSongs: () => invoke("list-songs"),
  openSong: (songId) => invoke("open-song", songId),
  openSongFolder: () => invoke("open-song-folder"),
  closeSong: () => invoke("close-song"),
  createSong: (input) => invoke("create-song", input),
  refreshSong: () => invoke("refresh-song"),
  review: (input) => invoke("review", input),
  setFinal: (versionId) => invoke("set-final", versionId),
  undoFinal: () => invoke("undo-final"),
  exportFinal: () => invoke("export-final"),
  revise: (input) => invoke("revise", input),
  draft: (query, instrumental, durationSeconds) => invoke("draft", query, instrumental, durationSeconds),
  plan: (input) => invoke("plan", input),
  applyPlan: (plan, feedbackText) => invoke("apply-plan", plan, feedbackText),
  generateMore: (count) => invoke("generate-more", count),
  resume: (jobId) => invoke("resume", jobId),
  cancelTask: () => invoke("cancel-task"),
  reveal: (target) => invoke("reveal", target),
  saveSettings: (partial) => invoke("save-settings", partial),
  pickProjectsDir: () => invoke("pick-projects-dir"),
  listAssistantModels: (kind, baseUrl) => invoke("assistant-models", kind, baseUrl),
  startEngine: () => invoke("engine-start"),
  stopEngine: () => invoke("engine-stop"),
  checkEngine: () => invoke("engine-check"),
  onEvent: (listener) => {
    const handler = (_event: unknown, payload: MusicEvent) => listener(payload);
    ipcRenderer.on("music:event", handler);
    return () => ipcRenderer.removeListener("music:event", handler);
  },
};

contextBridge.exposeInMainWorld("musicApp", Object.freeze(api));

declare global {
  interface Window {
    musicApp: MusicAppApi;
  }
}
