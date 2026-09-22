import { contextBridge, ipcRenderer } from "electron";
import type { MusicAppApi, ReviewInput } from "./shared.ts";

const api: MusicAppApi = {
  chooseProject: () => ipcRenderer.invoke("music:choose-project"),
  getState: () => ipcRenderer.invoke("music:get-state"),
  refresh: () => ipcRenderer.invoke("music:refresh"),
  review: (input: ReviewInput) => ipcRenderer.invoke("music:review", input),
  select: (candidateId: string) => ipcRenderer.invoke("music:select", candidateId),
  undoSelection: () => ipcRenderer.invoke("music:undo-selection"),
};

contextBridge.exposeInMainWorld("musicApp", Object.freeze(api));

declare global {
  interface Window {
    musicApp: MusicAppApi;
  }
}
