import type { SongState, Version } from "../shared.ts";
import { beginSongOpen, finishSongOpen, get, isOpeningSong, isSongTicket, listeningRevision, set, songTicket, type SongTicket, type View } from "./store.ts";
import { errorText, toast } from "./ui.ts";
import { isAutomaticAttempt } from "./quality.ts";
import { record, step } from "./history.ts";

export const api = window.musicApp;

type GoOptions = { record?: boolean };

export function go(view: View, options: GoOptions = {}): void {
  if (view === "studio" && get().song.status !== "ready") return;
  set({ view });
  if (options.record !== false) record({ view, songId: view === "studio" ? get().song.song?.songId ?? null : null });
  document.getElementById("views")?.scrollTo({ top: 0 });
}

// ⌘[ / ⌘] and the sidebar arrows. A studio entry for another song reopens that song.
export async function navigateHistory(direction: -1 | 1): Promise<void> {
  const route = step(direction);
  if (!route) return;
  if (route.view === "studio" && route.songId && route.songId !== get().song.song?.songId) {
    await openSong(route.songId, { record: false });
    return;
  }
  go(route.view, { record: false });
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
    active = next.song?.finalVersionId ?? next.song?.recommendedVersionId ?? versions.findLast((version) => !isAutomaticAttempt(version))?.id ?? null;
  }
  set({
    song: next,
    activeVersionId: active,
    ...(sameSong
      ? {}
      : { selection: null, plan: null, feedback: "", listenToParent: false, comparisonVersionId: null, scope: "song" as const, tab: "prompt" as const }),
  }, true);
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
  const revision = listeningRevision();
  try {
    const next = await api.refreshSong(songId);
    applySongForContext(next, ticket, get().activeVersionId === versionId && listeningRevision() === revision ? focusVersionId : undefined);
  } catch (error) {
    if (isSongTicket(ticket)) toast(errorText(error), { tone: "error" });
  }
}

export async function openSong(songId: string, options: GoOptions = {}): Promise<void> {
  // Reopening the song already on screen keeps its playback, selection and draft request.
  if (!isOpeningSong() && get().song.song?.songId === songId && get().song.status === "ready") {
    go("studio", options);
    return;
  }
  const request = beginSongOpen();
  try {
    const opened = await api.openSong(songId);
    if (!finishSongOpen(request) || !opened) return;
    applySong(opened);
    go("studio", options);
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
  const engine = get().engine;
  return engine.engine === "ace-step" && engine.capabilities.text2music
    && (engine.state === "ready" || engine.state === "external");
}

export function songBusy(): boolean {
  const state = get();
  return Boolean(state.song.song?.generationActive || (state.task && state.song.song && state.task.songId === state.song.song.songId));
}
