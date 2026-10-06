// Where the window was and how it was shown, so the app reopens the way it was left
// (most people keep it full screen). Pure functions; main.ts supplies the displays.

export type Rect = { x: number; y: number; width: number; height: number };
export type WindowState = { bounds: Rect; fullscreen: boolean; maximized: boolean };

export const MIN_WIDTH = 1080;
export const MIN_HEIGHT = 700;
// How much of a saved window must still be on some display to be trusted.
const VISIBLE = 160;

function rect(value: unknown): Rect | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const numbers = [raw.x, raw.y, raw.width, raw.height];
  if (!numbers.every((item) => typeof item === "number" && Number.isFinite(item))) return null;
  return { x: Math.round(raw.x as number), y: Math.round(raw.y as number), width: Math.round(raw.width as number), height: Math.round(raw.height as number) };
}

export function parseWindowState(value: unknown): WindowState | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const bounds = rect(raw.bounds);
  if (!bounds) return null;
  return { bounds, fullscreen: raw.fullscreen === true, maximized: raw.maximized === true };
}

function overlap(a: Rect, b: Rect): { width: number; height: number } {
  return {
    width: Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x),
    height: Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y),
  };
}

// The saved bounds when they still land on a connected display, otherwise the primary
// display's work area: a first launch opens as large as the screen allows.
export function windowBounds(saved: Rect | null, workAreas: Rect[], primary: Rect): Rect {
  if (saved && saved.width >= MIN_WIDTH && saved.height >= MIN_HEIGHT) {
    const visible = workAreas.some((area) => {
      const shared = overlap(saved, area);
      return shared.width >= VISIBLE && shared.height >= VISIBLE;
    });
    if (visible) return saved;
  }
  return { x: primary.x, y: primary.y, width: Math.max(MIN_WIDTH, primary.width), height: Math.max(MIN_HEIGHT, primary.height) };
}
