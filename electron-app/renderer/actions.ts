import type { SongState, Version } from "../shared.ts";
import { beginSongOpen, finishSongOpen, get, isSongTicket, set, songTicket, type SongTicket, type View } from "./store.ts";
import { errorText, toast } from "./ui.ts";

export const api = window.musicApp;

export function go(view: View): void {
  if (view === "studio" && get().song.status !== "ready") return;
  set({ view });
  document.getElementById("content")?.scrollTo({ top: 0 });
}

export function activeVersion(): Version | null {
  const state = get();
  return state.song.song?.versions.find((item) => item.id === state.activeVersionId) ?? null;
}

// Keep the listener's place when the same song refreshes; reset per-song UI otherwise.
export function applySong(next: SongState, focusVersionId?: string | null): void {
  const state = get();
  const versions = next.song?.versions ?? [];
  const sameSong = state.song.song?.songId === next.song?.songId;
  let active = focusVersionId ?? (sameSong ? state.activeVersionId : null);
  if (!versions.some((item) => item.id === active)) {
    active = next.song?.finalVersionId ?? versions.at(-1)?.id ?? null;
  }
  set({
    song: next,
    activeVersionId: active,
    ...(sameSong
      ? {}
      : { selection: null, plan: null, feedback: "", listenToParent: false, scope: "song" as const, tab: "fix" as const }),
  });
  if (next.status === "error" && next.error) toast(next.error, { tone: "error" });
}

export function applySongForContext(next: SongState, ticket: SongTicket | null, focusVersionId?: string | null): boolean {
  if (!isSongTicket(ticket) || (next.song && next.song.songId !== ticket.songId)) return false;
  applySong(next, focusVersionId);
  return true;
}

export async function refreshSong(songId: string, focusVersionId?: string | null): Promise<void> {
  const ticket = songTicket(songId);
  if (!ticket) return;
  const versionId = get().activeVersionId;
  try {
    const next = await api.refreshSong(songId);
    applySongForContext(next, ticket, get().activeVersionId === versionId ? focusVersionId : undefined);
  } catch (error) {
    if (isSongTicket(ticket)) toast(errorText(error), { tone: "error" });
  }
}

export async function openSong(songId: string): Promise<void> {
  const request = beginSongOpen();
  try {
    const opened = await api.openSong(songId);
    if (!finishSongOpen(request) || !opened) return;
    applySong(opened);
    go("studio");
  } catch (error) {
    if (finishSongOpen(request)) toast(errorText(error), { tone: "error" });
  }
}

export async function openFolderDialog(): Promise<void> {
  const request = beginSongOpen();
  try {
    const opened = await api.openSongFolder();
    if (!finishSongOpen(request) || !opened) return;
    applySong(opened);
    go("studio");
  } catch (error) {
    if (finishSongOpen(request)) toast(errorText(error), { tone: "error" });
  }
}

export async function refreshSongs(): Promise<void> {
  try {
    set({ songs: await api.listSongs() });
  } catch (error) {
    toast(errorText(error), { tone: "error" });
  }
}

export async function startEngine(): Promise<void> {
  try {
    set({ engine: await api.startEngine() });
  } catch (error) {
    toast(errorText(error), { tone: "error" });
  }
}

export function engineReady(): boolean {
  const state = get().engine.state;
  return state === "ready" || state === "external";
}

export function songBusy(): boolean {
  const state = get();
  return Boolean(state.song.song?.generationActive || (state.task && state.song.song && state.task.songId === state.song.song.songId));
}
