// Seamless playback of a BGM version's loop. An <audio loop> element leaves a small gap
// when it wraps, which is exactly what this preview must not do, so the loop file is
// decoded once and played by Web Audio with loop = true, at the file's own sample rate
// (no resampling, so the wrap is sample-exact). Playback starts a few seconds before the
// seam so the first thing heard is the end running into the start.

export const SEAM_LEAD_SECONDS = 6;

/** Where in the loop to start so the seam comes after ``lead`` seconds. */
export function previewOffset(durationSeconds: number, lead = SEAM_LEAD_SECONDS): number {
  return durationSeconds > lead ? durationSeconds - lead : 0;
}

/** The position inside the loop after ``elapsed`` seconds of playback from ``offset``. */
export function loopPosition(durationSeconds: number, offset: number, elapsed: number): number {
  if (!(durationSeconds > 0)) return 0;
  return (offset + elapsed) % durationSeconds;
}

export class LoopPreview {
  private context: AudioContext | null = null;
  private source: AudioBufferSourceNode | null = null;
  private buffer: { url: string; data: AudioBuffer } | null = null;
  private serial = 0;
  playingUrl: string | null = null;
  onChange: () => void = () => undefined;

  async start(url: string, sampleRate: number): Promise<void> {
    this.stop();
    const serial = ++this.serial;
    if (!this.context || this.context.sampleRate !== sampleRate) {
      void this.context?.close();
      this.context = new AudioContext({ sampleRate });
    }
    const context = this.context;
    if (this.buffer?.url !== url) {
      const response = await fetch(url);
      if (!response.ok) throw new Error("반복용 파일을 읽지 못했어요.");
      const data = await context.decodeAudioData(await response.arrayBuffer());
      if (serial !== this.serial) return;
      this.buffer = { url, data };
    }
    if (serial !== this.serial) return;
    await context.resume();
    const source = context.createBufferSource();
    source.buffer = this.buffer.data;
    source.loop = true;
    source.connect(context.destination);
    source.start(0, previewOffset(this.buffer.data.duration));
    this.source = source;
    this.playingUrl = url;
    this.onChange();
  }

  stop(): void {
    this.serial += 1;
    if (this.source) {
      try { this.source.stop(); } catch { /* already stopped */ }
      this.source.disconnect();
      this.source = null;
    }
    if (this.playingUrl) {
      this.playingUrl = null;
      this.onChange();
    }
  }

  get playing(): boolean {
    return this.playingUrl !== null;
  }
}
