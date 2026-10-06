import type { TimeRange } from "../shared.ts";
import { clock } from "./format.ts";
import { audibleSelection } from "./comparison.ts";

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
// The narrowest view, in seconds. With ~4,800 waveform points a 3-minute take still
// shows its own detail at this width.
const MIN_VIEW_SECONDS = 3;
const MAX_ZOOM = 48;

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// One audio element for the whole studio. Switching versions keeps the playhead where
// it was, so two takes can be compared at the same playback time.
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
  // Zoomed view: the visible window starts at viewStart and spans viewSpan seconds.
  // viewSpan 0 means the whole take, which is also what a new song opens with.
  private viewStart = 0;
  private viewSpan = 0;
  private overview: HTMLCanvasElement | null = null;
  private overviewDrag = false;
  onSelection: (range: TimeRange | null) => void = () => undefined;
  onTick: () => void = () => undefined;
  onView: () => void = () => undefined;

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
    // ⌘/Ctrl + wheel (and trackpad pinch, which arrives as ctrl + wheel) zooms around the
    // pointer. Sideways scrolling, or shift + wheel, pans a zoomed view. Plain vertical
    // scrolling is left to the page.
    canvas.addEventListener("wheel", (event) => {
      if (!this.length) return;
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        this.zoomBy(Math.exp(-event.deltaY * 0.01), this.timeAt(event.clientX));
        return;
      }
      const sideways = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.shiftKey ? event.deltaY : 0;
      if (this.zoom > 1 && sideways) {
        event.preventDefault();
        const rect = this.canvas.getBoundingClientRect();
        this.pan((sideways / Math.max(1, rect.width)) * this.span());
      }
    }, { passive: false });
  }

  // A small whole-song strip under the waveform; dragging it moves the zoomed window.
  attachOverview(canvas: HTMLCanvasElement): void {
    this.overview = canvas;
    const move = (event: PointerEvent) => {
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
      this.centerOn(ratio * this.length);
    };
    canvas.addEventListener("pointerdown", (event) => {
      if (!this.length || event.button !== 0) return;
      canvas.setPointerCapture(event.pointerId);
      this.overviewDrag = true;
      move(event);
    });
    canvas.addEventListener("pointermove", (event) => {
      if (this.overviewDrag) move(event);
    });
    const end = () => {
      this.overviewDrag = false;
    };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    new ResizeObserver(() => this.draw()).observe(canvas);
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

  get zoom(): number {
    const length = this.length;
    return length && this.viewSpan ? length / this.span() : 1;
  }

  get viewRange(): TimeRange {
    const start = this.start();
    return { startSeconds: start, endSeconds: start + this.span() };
  }

  private span(): number {
    const length = this.length;
    return this.viewSpan > 0 && length ? Math.min(this.viewSpan, length) : length;
  }

  private start(): number {
    return Math.max(0, Math.min(this.viewStart, this.length - this.span()));
  }

  private maxZoom(): number {
    return Math.max(1, Math.min(MAX_ZOOM, this.length / MIN_VIEW_SECONDS));
  }

  // Zoom to `zoom`× keeping `anchor` (seconds) under the same spot on screen.
  setZoom(zoom: number, anchor?: number): void {
    const length = this.length;
    if (!length) return;
    const next = Math.max(1, Math.min(this.maxZoom(), zoom));
    const oldStart = this.start();
    const oldSpan = this.span();
    const focus = anchor ?? (this.time >= oldStart && this.time <= oldStart + oldSpan ? this.time : oldStart + oldSpan / 2);
    const place = oldSpan ? Math.max(0, Math.min(1, (focus - oldStart) / oldSpan)) : 0.5;
    if (next <= 1.001) {
      this.viewSpan = 0;
      this.viewStart = 0;
    } else {
      this.viewSpan = length / next;
      this.viewStart = Math.max(0, Math.min(length - this.viewSpan, focus - place * this.viewSpan));
    }
    this.draw();
    this.onView();
  }

  zoomBy(factor: number, anchor?: number): void {
    this.setZoom(this.zoom * factor, anchor);
  }

  showWhole(): void {
    this.setZoom(1);
  }

  // Frame the selection with a little room on both sides so its edges can be dragged.
  fitSelection(): void {
    const length = this.length;
    const range = this.selection;
    if (!length || !range) return;
    const width = Math.max(MIN_VIEW_SECONDS, (range.endSeconds - range.startSeconds) * 1.4);
    const zoom = Math.max(1, Math.min(this.maxZoom(), length / width));
    if (zoom <= 1.001) {
      this.showWhole();
      return;
    }
    this.viewSpan = length / zoom;
    const middle = (range.startSeconds + range.endSeconds) / 2;
    this.viewStart = Math.max(0, Math.min(length - this.viewSpan, middle - this.viewSpan / 2));
    this.draw();
    this.onView();
  }

  pan(seconds: number): void {
    if (!this.viewSpan) return;
    this.viewStart = Math.max(0, Math.min(this.length - this.span(), this.start() + seconds));
    this.draw();
    this.onView();
  }

  private centerOn(seconds: number): void {
    if (!this.viewSpan) return;
    this.viewStart = Math.max(0, Math.min(this.length - this.span(), seconds - this.span() / 2));
    this.draw();
    this.onView();
  }

  // While zoomed, keep the playhead on screen by turning the page when it leaves.
  private follow(time: number): void {
    if (!this.viewSpan || this.drag || this.overviewDrag) return;
    const start = this.start();
    const span = this.span();
    if (time >= start && time <= start + span) return;
    this.viewStart = Math.max(0, Math.min(this.length - span, time - span * 0.05));
    this.onView();
  }

  async load(source: PlayerSource | null): Promise<void> {
    if (source && source.key === this.key && source.url === this.sourceUrl) {
      this.peaks = source.peaks;
      this.duration = source.duration ?? 0;
      this.draw();
      return;
    }
    const previousTime = this.pendingSeek ?? this.time;
    const wasPlaying = this.playing || this.playWhenReady;
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
    this.pendingSeek = previousTime;
    this.playWhenReady = wasPlaying;
    this.key = source.key;
    this.peaks = source.peaks;
    this.duration = source.duration ?? 0;
    this.audio.pause();
    if (!source.url) {
      this.pendingSeek = null;
      this.playWhenReady = false;
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
    if (this.playWhenReady) await this.audio.play().catch(() => undefined);
    if (serial !== this.loadSerial) return;
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
      const range = audibleSelection(this.selection, this.length);
      if (this.loop && range && (this.time < range.startSeconds || this.time >= range.endSeconds)) {
        this.audio.currentTime = range.startSeconds;
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
    if (this.viewSpan && !this.drag) {
      const start = this.start();
      const span = this.span();
      if (time < start || time > start + span) this.centerOn(time);
    }
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
    const range = audibleSelection(this.selection, this.length);
    if (this.loop && range && this.playing && this.time >= range.endSeconds - 0.02) {
      this.audio.currentTime = range.startSeconds;
    }
  }

  private startFrames(): void {
    cancelAnimationFrame(this.frame);
    const step = () => {
      this.enforceLoop();
      this.follow(this.time);
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
    return this.start() + ratio * this.span();
  }

  private xOf(seconds: number): number {
    const rect = this.canvas.getBoundingClientRect();
    const span = this.span();
    return span ? ((seconds - this.start()) / span) * rect.width : 0;
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
        startSeconds: Math.round(this.selection.startSeconds * 100) / 100,
        endSeconds: Math.round(this.selection.endSeconds * 100) / 100,
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
    const viewStart = this.start();
    const viewSpan = this.span();
    const toX = (seconds: number) => (viewSpan ? ((seconds - viewStart) / viewSpan) * width : 0);
    const precise = this.zoom >= 4 ? 2 : 1;

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
      const now = this.time;
      const peaks = this.peaks;
      const binAt = (seconds: number) => (length ? Math.floor((seconds / length) * peaks.length) : 0);
      // Scale to the loudest moment so quiet takes stay readable; shape, not level, is the point.
      let loudest = 0;
      for (const value of peaks) if (value > loudest) loudest = value;
      const scale = loudest > 0.02 ? 1 / loudest : 1;
      for (let index = 0; index < count; index += 1) {
        const from = binAt(viewStart + (index / count) * viewSpan);
        const to = Math.max(from + 1, binAt(viewStart + ((index + 1) / count) * viewSpan));
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
        const seconds = viewStart + ((x + barWidth / 2) / width) * viewSpan;
        const played = seconds <= now;
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
      const startLabel = clock(selection.startSeconds, true, precise);
      const endLabel = clock(selection.endSeconds, true, precise);
      ctx.fillText(startLabel, Math.max(4 * ratio, Math.min(x0 + 5 * ratio, width - 90 * ratio)), 13 * ratio);
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
      const label = clock(viewStart + (this.hoverX / rect.width) * viewSpan, true, precise);
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
    this.drawOverview();
  }

  private drawOverview(): void {
    const canvas = this.overview;
    if (!canvas || !this.viewSpan) return;
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round(rect.width * ratio));
    const height = Math.max(1, Math.round(rect.height * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const ctx = canvas.getContext("2d");
    const length = this.length;
    if (!ctx || !length) return;
    const p = this.palette;
    const toX = (seconds: number) => (seconds / length) * width;
    ctx.clearRect(0, 0, width, height);
    const peaks = this.peaks;
    const middle = height / 2;
    let loudest = 0;
    for (const value of peaks) if (value > loudest) loudest = value;
    const scale = loudest > 0.02 ? 1 / loudest : 1;
    const step = Math.max(2, Math.round(2 * ratio));
    for (let x = 0; x < width; x += step) {
      const from = Math.floor((x / width) * peaks.length);
      const to = Math.max(from + 1, Math.floor(((x + step) / width) * peaks.length));
      let peak = 0;
      for (let bin = from; bin < to && bin < peaks.length; bin += 1) if (peaks[bin] > peak) peak = peaks[bin];
      const amplitude = Math.max(ratio / 2, Math.min(1, peak * scale) * middle * 0.9);
      ctx.fillStyle = toX(this.time) >= x ? p.accent : p.muted;
      ctx.fillRect(x, middle - amplitude, Math.max(1, step - ratio), amplitude * 2);
    }
    if (this.selection) {
      ctx.fillStyle = p.selection;
      ctx.fillRect(toX(this.selection.startSeconds), 0, toX(this.selection.endSeconds) - toX(this.selection.startSeconds), height);
    }
    const x0 = toX(this.start());
    const x1 = toX(this.start() + this.span());
    ctx.fillStyle = "rgba(238, 242, 231, .10)";
    ctx.fillRect(x0, 0, x1 - x0, height);
    ctx.strokeStyle = p.accent;
    ctx.lineWidth = 1.5 * ratio;
    ctx.strokeRect(x0 + ratio, ratio, Math.max(2 * ratio, x1 - x0 - 2 * ratio), height - 2 * ratio);
  }
}
