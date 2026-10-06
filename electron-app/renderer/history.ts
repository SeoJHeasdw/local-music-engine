import type { View } from "./store.ts";

// Back/forward over the screens the person actually visited, like a browser. A studio
// entry remembers which song was open, so going back can reopen that song.
export type Route = { view: View; songId: string | null };

const LIMIT = 100;
let stack: Route[] = [];
let index = -1;
const listeners = new Set<() => void>();

function same(a: Route | undefined, b: Route): boolean {
  return Boolean(a && a.view === b.view && a.songId === b.songId);
}

function notify(): void {
  for (const listener of listeners) listener();
}

export function record(route: Route): void {
  if (same(stack[index], route)) return;
  stack = [...stack.slice(0, index + 1), route].slice(-LIMIT);
  index = stack.length - 1;
  notify();
}

export function canGoBack(): boolean {
  return index > 0;
}

export function canGoForward(): boolean {
  return index < stack.length - 1;
}

// Moves the cursor and returns where to go. The caller shows that screen without
// recording it again.
export function step(direction: -1 | 1): Route | null {
  const next = index + direction;
  if (next < 0 || next >= stack.length) return null;
  index = next;
  notify();
  return stack[index];
}

export function onHistoryChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function resetHistory(route: Route | null = null): void {
  stack = route ? [route] : [];
  index = stack.length - 1;
  notify();
}
