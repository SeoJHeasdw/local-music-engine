import type { TimeRange } from "../shared.ts";
import { clock } from "./format.ts";

export type PlayerSource = {
  key: string;
  url: string | null;
  peaks: number[];
  duration: number | null;
};

type Drag =
  | { mode: "pending"; originX: number; originTime: number }
  | { mode: "create"; anchor: number }
  | { mode: "start" | "end" };

const MIN_SELECTION = 0.5;

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// One audio element for the whole studio. Switching versions keeps the playhead where
// it was, so two takes can be compared at the same bar.
export class Player {
  readonly audio = new Audio();
  private context: CanvasRenderingContext2D;
  private peaks: number[] = [];
  private duration = 0;
  private key: string | null = null;
  private frame = 0;
  private hoverX: number | null = null;
  private drag: Drag | null = null;
  private palette = this.readPalette();
  private loadSerial = 0;
  private sourceUrl: string | null = null;
  private cancelLoad: (() => void) | null = null;
  private pendingSeek: number | null = null;
  private playWhenReady = false;
  selection: TimeRange | null = null;
  editRange: TimeRange | null = null;
  loop = false;
  overlay: string | null = null;
  unavailable: string | null = null;
  onSelection: (range: TimeRange | null) => void = () => undefined;
  onTick: () => void = () => undefined;

  constructor(private canvas: HTMLCanvasElement) {
    const context = canvas.getContext("2d");
    if (!context) throw new Error("canvas unavailable");
    this.context = context;
    this.audio.preload = "auto";
    for (const name of ["play", "pause", "ended", "loadedmetadata", "seeked"]) {
      this.audio.addEventListener(name, () => {
        if (name === "play") this.startFrames();
        this.draw();
        this.onTick();
      });
    }
    this.audio.addEventListener("timeupdate", () => {
      this.enforceLoop();
      if (this.audio.paused) {
        this.draw();
        this.onTick();
      }
    });
    new ResizeObserver(() => this.draw()).observe(canvas);
    canvas.addEventListener("pointerdown", (event) => this.pointerDown(event));
    canvas.addEventListener("pointermove", (event) => this.pointerMove(event));
    canvas.addEventListener("pointerup", (event) => this.pointerUp(event));
    canvas.addEventListener("pointercancel", () => {
      this.drag = null;
      this.draw();
    });
    canvas.addEventListener("pointerleave", () => {
      this.hoverX = null;
      if (!this.drag) this.draw();
    });
  }

  private readPalette() {
    return {
      accent: token("--accent") || "#7cc4ff",
      accentHi: token("--accent-hi") || "#a9d8ff",
      muted: token("--wave-muted") || "rgba(238,242,231,.26)",
      mutedHi: token("--wave-muted-hi") || "rgba(238,242,231,.5)",
      selection: token("--accent-soft") || "rgba(124,196,255,.12)",
      edit: token("--edit") || "#c6a8ff",
      editSoft: token("--edit-soft") || "rgba(198,168,255,.12)",
      text: token("--text") || "#eef1ea",
      text3: token("--text-3") || "#8d9587",
      line: token("--line") || "rgba(238,242,231,.1)",
      well: token("--well") || "#0b0d0a",
      mono: token("--mono") || "ui-monospace, monospace",
    };
  }

  get time(): number {
    return this.audio.currentTime || 0;
  }

  get length(): number {
    return Number.isFinite(this.audio.duration) && this.audio.duration > 0 ? this.audio.duration : this.duration;
  }

  get playing(): boolean {
    return !this.audio.paused;
  }

  async load(source: PlayerSource | null): Promise<void> {
    if (source && source.key === this.key && source.url === this.sourceUrl) {
      this.peaks = source.peaks;
      this.duration = source.duration ?? 0;
      this.draw();
      return;
    }
    const serial = ++this.loadSerial;
    this.cancelLoad?.();
    this.cancelLoad = null;
    this.pendingSeek = null;
    this.playWhenReady = false;
    this.sourceUrl = source?.url ?? null;
    if (!source) {
      this.key = null;
      this.peaks = [];
      this.duration = 0;
      this.audio.pause();
      this.audio.removeAttribute("src");
      this.audio.load();
      this.draw();
      this.onTick();
      return;
    }
    const previousTime = this.time;
    const wasPlaying = this.playing;
    this.key = source.key;
    this.peaks = source.peaks;
    this.duration = source.duration ?? 0;
    this.audio.pause();
    if (!source.url) {
      this.audio.removeAttribute("src");
      this.audio.load();
      this.draw();
      this.onTick();
      return;
    }
    await new Promise<void>((resolve) => {
      const done = () => {
        this.audio.removeEventListener("loadedmetadata", done);
        this.audio.removeEventListener("error", done);
        if (this.cancelLoad === done) this.cancelLoad = null;
        resolve();
      };
      this.cancelLoad = done;
      this.audio.addEventListener("loadedmetadata", done, { once: true });
      this.audio.addEventListener("error", done, { once: true });
      this.audio.src = source.url!;
    });
    if (serial !== this.loadSerial) return;
    const end = Math.max(0, this.length - 0.05);
    this.audio.currentTime = Math.min(this.pendingSeek ?? previousTime, end);
    this.pendingSeek = null;
    if (wasPlaying || this.playWhenReady) await this.audio.play().catch(() => undefined);
    this.playWhenReady = false;
    this.draw();
    this.onTick();
  }

  toggle(): void {
    if (!this.audio.src) return;
    if (this.audio.readyState < 1) {
      this.playWhenReady = !this.playWhenReady;
      return;
    }
    if (this.playing) this.audio.pause();
    else {
      if (this.loop && this.selection && (this.time < this.selection.startSeconds || this.time >= this.selection.endSeconds)) {
        this.audio.currentTime = this.selection.startSeconds;
      } else if (this.time >= this.length - 0.05) {
        this.audio.currentTime = 0;
      }
      void this.audio.play().catch(() => undefined);
    }
  }

  seek(seconds: number): void {
    if (!this.audio.src) return;
    const time = Math.max(0, Math.min(this.length - 0.02, seconds));
    if (this.audio.readyState < 1) this.pendingSeek = time;
    else this.audio.currentTime = time;
    this.draw();
    this.onTick();
  }

  nudge(delta: number): void {
    this.seek(this.time + delta);
  }

  setSelection(range: TimeRange | null): void {
    this.selection = range;
    this.draw();
  }

  private enforceLoop(): void {
    const range = this.selection;
    if (this.loop && range && this.playing && this.time >= range.endSeconds - 0.02) {
      this.audio.currentTime = range.startSeconds;
    }
  }

  private startFrames(): void {
    cancelAnimationFrame(this.frame);
    const step = () => {
      this.enforceLoop();
      this.draw();
      this.onTick();
      if (this.playing) this.frame = requestAnimationFrame(step);
    };
    this.frame = requestAnimationFrame(step);
  }

  // ---- pointer ----
  private timeAt(clientX: number): number {
    const rect = this.canvas.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
    return ratio * this.length;
  }

  private xOf(seconds: number): number {
    const rect = this.canvas.getBoundingClientRect();
    return this.length ? (seconds / this.length) * rect.width : 0;
  }

  private edgeAt(clientX: number): "start" | "end" | null {
    if (!this.selection) return null;
    const rect = this.canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    if (Math.abs(x - this.xOf(this.selection.startSeconds)) <= 7) return "start";
    if (Math.abs(x - this.xOf(this.selection.endSeconds)) <= 7) return "end";
    return null;
  }

  private pointerDown(event: PointerEvent): void {
    if (!this.length || event.button !== 0) return;
    this.canvas.setPointerCapture(event.pointerId);
    const edge = this.edgeAt(event.clientX);
    this.drag = edge ? { mode: edge } : { mode: "pending", originX: event.clientX, originTime: this.timeAt(event.clientX) };
  }

  private pointerMove(event: PointerEvent): void {
    const rect = this.canvas.getBoundingClientRect();
    this.hoverX = event.clientX - rect.left;
    const drag = this.drag;
    if (!drag) {
      this.canvas.style.cursor = this.edgeAt(event.clientX) ? "ew-resize" : "crosshair";
      this.draw();
      return;
    }
    const time = this.timeAt(event.clientX);
    if (drag.mode === "pending") {
      if (Math.abs(event.clientX - drag.originX) < 4) return;
      this.drag = { mode: "create", anchor: drag.originTime };
    }
    const current = this.drag;
    if (current?.mode === "create") {
      this.selection = { startSeconds: Math.min(current.anchor, time), endSeconds: Math.max(current.anchor, time) };
    } else if (current?.mode === "start" && this.selection) {
      this.selection = { ...this.selection, startSeconds: Math.min(time, this.selection.endSeconds - 0.3) };
    } else if (current?.mode === "end" && this.selection) {
      this.selection = { ...this.selection, endSeconds: Math.max(time, this.selection.startSeconds + 0.3) };
    }
    this.draw();
  }

  private pointerUp(event: PointerEvent): void {
    const drag = this.drag;
    this.drag = null;
    if (!drag) return;
    if (drag.mode === "pending") {
      this.seek(drag.originTime);
      return;
    }
    const range = this.selection;
    if (range && range.endSeconds - range.startSeconds < MIN_SELECTION) this.selection = null;
    if (this.selection) {
      this.selection = {
        startSeconds: Math.round(this.selection.startSeconds * 10) / 10,
        endSeconds: Math.round(this.selection.endSeconds * 10) / 10,
      };
    }
    this.draw();
    this.onSelection(this.selection);
    event.preventDefault();
  }

  // ---- drawing ----
  draw(): void {
    const canvas = this.canvas;
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round(rect.width * ratio));
    const height = Math.max(1, Math.round(rect.height * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const ctx = this.context;
    const p = this.palette;
    ctx.clearRect(0, 0, width, height);
    const top = 22 * ratio;
    const bottom = height - 4 * ratio;
    const middle = (top + bottom) / 2;
    const half = (bottom - top) / 2;
    const length = this.length;
    const toX = (seconds: number) => (length ? (seconds / length) * width : 0);

    ctx.fillStyle = p.line;
    ctx.fillRect(0, Math.round(middle), width, Math.max(1, Math.round(ratio)));

    if (this.editRange && length) {
      const x0 = toX(this.editRange.startSeconds);
      const x1 = toX(this.editRange.endSeconds);
      ctx.fillStyle = p.editSoft;
      ctx.fillRect(x0, top - 6 * ratio, x1 - x0, bottom - top + 6 * ratio);
      ctx.fillStyle = p.edit;
      ctx.fillRect(x0, top - 6 * ratio, x1 - x0, 2 * ratio);
      ctx.font = `600 ${10.5 * ratio}px ${getComputedStyle(document.body).fontFamily}`;
      ctx.textBaseline = "alphabetic";
      ctx.textAlign = "left";
      if (x1 - x0 > 64 * ratio) ctx.fillText("수정한 구간", x0 + 6 * ratio, bottom - 6 * ratio);
    }

    const selection = this.selection;
    if (selection && length) {
      const x0 = toX(selection.startSeconds);
      const x1 = toX(selection.endSeconds);
      ctx.fillStyle = p.selection;
      ctx.fillRect(x0, top - 6 * ratio, x1 - x0, bottom - top + 6 * ratio);
    }

    if (!this.peaks.length) {
      ctx.fillStyle = p.text3;
      ctx.font = `${13 * ratio}px ${getComputedStyle(document.body).fontFamily}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(this.unavailable ?? "재생할 버전을 고르세요", width / 2, middle);
    } else {
      const barWidth = Math.max(2, Math.round(3 * ratio));
      const gap = Math.max(1, Math.round(1.5 * ratio));
      const step = barWidth + gap;
      const count = Math.floor(width / step);
      const progress = length ? this.time / length : 0;
      const peaks = this.peaks;
      // Scale to the loudest moment so quiet takes stay readable; shape, not level, is the point.
      let loudest = 0;
      for (const value of peaks) if (value > loudest) loudest = value;
      const scale = loudest > 0.02 ? 1 / loudest : 1;
      for (let index = 0; index < count; index += 1) {
        const from = Math.floor((index / count) * peaks.length);
        const to = Math.max(from + 1, Math.floor(((index + 1) / count) * peaks.length));
        const peakAt = (start: number, end: number) => {
          let value = 0;
          for (let bin = Math.max(0, start); bin < end && bin < peaks.length; bin += 1) if (peaks[bin] > value) value = peaks[bin];
          return value;
        };
        const span = to - from;
        // A touch of the neighbours keeps drum transients from reading as a comb on short takes.
        const peak = Math.max(peakAt(from, to), 0.72 * Math.max(peakAt(from - span, from), peakAt(to, to + span)));
        const amplitude = Math.max(ratio, Math.pow(Math.min(1, peak * scale), 0.9) * half * 0.96);
        const x = index * step;
        const seconds = ((x + barWidth / 2) / width) * length;
        const played = (x + barWidth / 2) / width <= progress;
        const inside = selection ? seconds >= selection.startSeconds && seconds <= selection.endSeconds : false;
        ctx.fillStyle = played ? (inside ? p.accentHi : p.accent) : inside ? p.mutedHi : p.muted;
        ctx.beginPath();
        ctx.roundRect(x, middle - amplitude, barWidth, amplitude * 2, barWidth / 2);
        ctx.fill();
      }
    }

    ctx.font = `500 ${10.5 * ratio}px ${p.mono}`;
    ctx.textBaseline = "alphabetic";
    if (selection && length) {
      const x0 = toX(selection.startSeconds);
      const x1 = toX(selection.endSeconds);
      ctx.fillStyle = p.accent;
      ctx.fillRect(x0 - ratio, top - 6 * ratio, 2 * ratio, bottom - top + 6 * ratio);
      ctx.fillRect(x1 - ratio, top - 6 * ratio, 2 * ratio, bottom - top + 6 * ratio);
      for (const x of [x0, x1]) {
        ctx.beginPath();
        ctx.roundRect(x - 3 * ratio, middle - 9 * ratio, 6 * ratio, 18 * ratio, 3 * ratio);
        ctx.fill();
      }
      ctx.textAlign = "left";
      const startLabel = clock(selection.startSeconds, true);
      const endLabel = clock(selection.endSeconds, true);
      ctx.fillText(startLabel, Math.min(x0 + 5 * ratio, width - 90 * ratio), 13 * ratio);
      ctx.textAlign = "right";
      const endWidth = ctx.measureText(endLabel).width;
      if (x1 - x0 > endWidth + 60 * ratio) ctx.fillText(endLabel, x1 - 5 * ratio, 13 * ratio);
    }

    if (length && this.audio.src) {
      const x = toX(this.time);
      ctx.fillStyle = p.text;
      ctx.fillRect(Math.round(x - ratio), top - 6 * ratio, Math.max(1, Math.round(2 * ratio)), bottom - top + 6 * ratio);
      ctx.beginPath();
      ctx.arc(x, top - 6 * ratio, 3.5 * ratio, 0, Math.PI * 2);
      ctx.fill();
    }

    if (this.hoverX !== null && length && !this.drag) {
      const x = this.hoverX * ratio;
      ctx.fillStyle = p.mutedHi;
      ctx.fillRect(Math.round(x), top, Math.max(1, Math.round(ratio)), bottom - top);
      const label = clock((this.hoverX / rect.width) * length, true);
      ctx.textAlign = x > width - 60 * ratio ? "right" : "left";
      ctx.fillStyle = p.text3;
      ctx.fillText(label, x + (x > width - 60 * ratio ? -6 : 6) * ratio, bottom - 6 * ratio);
    }

    if (this.overlay) {
      ctx.font = `600 ${11 * ratio}px ${getComputedStyle(document.body).fontFamily}`;
      ctx.textAlign = "right";
      ctx.fillStyle = p.edit;
      ctx.fillText(this.overlay, width - 4 * ratio, 13 * ratio);
    }
  }
}
