import { contextBridge, ipcRenderer } from "electron";
import type { MusicAppApi, MusicEvent } from "./shared.ts";

const invoke = (channel: string, ...args: unknown[]) => ipcRenderer.invoke(`music:${channel}`, ...args);

const api: MusicAppApi = {
  bootstrap: () => invoke("bootstrap"),
  listSongs: () => invoke("list-songs"),
  openSong: (songId) => invoke("open-song", songId),
  openSongFolder: () => invoke("open-song-folder"),
  closeSong: (songId) => invoke("close-song", songId),
  createSong: (input) => invoke("create-song", input),
  refreshSong: (songId) => invoke("refresh-song", songId),
  review: (input) => invoke("review", input),
  setFinal: (songId, versionId) => invoke("set-final", songId, versionId),
  undoFinal: (songId) => invoke("undo-final", songId),
  exportFinal: (songId) => invoke("export-final", songId),
  revise: (input) => invoke("revise", input),
  draft: (query, instrumental, durationSeconds, productionRules, vocalLanguage) => invoke("draft", query, instrumental, durationSeconds, productionRules, vocalLanguage),
  songPlan: (input) => invoke("song-plan", input),
  regenerateSong: (input) => invoke("regenerate-song", input),
  plan: (input) => invoke("plan", input),
  applyPlan: (songId, plan, feedbackText) => invoke("apply-plan", songId, plan, feedbackText),
  generateMore: (songId, count) => invoke("generate-more", songId, count),
  resume: (songId, jobId) => invoke("resume", songId, jobId),
  cancelTask: (songId, startedAt) => invoke("cancel-task", songId, startedAt),
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
