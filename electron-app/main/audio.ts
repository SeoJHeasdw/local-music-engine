import crypto from "node:crypto";
import { createReadStream } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import { Readable } from "node:stream";

// Audio reaches the renderer only through opaque tokens for files main already
// verified. A token stays stable for a path so a refresh never cuts off playback.
const tokenByPath = new Map<string, string>();
const pathByToken = new Map<string, string>();
const peaksCache = new Map<string, { size: number; mtimeMs: number; peaks: number[] }>();

export const ARTIFACT_SCHEME = "music-artifact";

export function audioUrlFor(filePath: string): string {
  let token = tokenByPath.get(filePath);
  if (!token) {
    token = crypto.randomBytes(24).toString("hex");
    tokenByPath.set(filePath, token);
    pathByToken.set(token, filePath);
  }
  return `${ARTIFACT_SCHEME}://artifact/${token}`;
}

export async function handleArtifactRequest(request: Request): Promise<Response> {
  const token = new URL(request.url).pathname.replace(/^\//, "");
  const filePath = pathByToken.get(token);
  if (!filePath) return new Response("Not found", { status: 404 });
  let size: number;
  try {
    size = (await stat(filePath)).size;
  } catch {
    return new Response("Not found", { status: 404 });
  }
  const headers = {
    "Accept-Ranges": "bytes",
    "Content-Type": "audio/wav",
    "Cache-Control": "no-store",
  };
  const match = request.headers.get("range")?.match(/^bytes=(\d*)-(\d*)$/);
  if (!match) {
    const body = Readable.toWeb(createReadStream(filePath)) as ReadableStream<Uint8Array>;
    return new Response(body, { status: 200, headers: { ...headers, "Content-Length": String(size) } });
  }
  let start: number;
  let end: number;
  if (!match[1] && match[2]) {
    // Suffix range: the last N bytes.
    start = Math.max(0, size - Number.parseInt(match[2], 10));
    end = size - 1;
  } else {
    start = match[1] ? Number.parseInt(match[1], 10) : 0;
    end = match[2] ? Math.min(size - 1, Number.parseInt(match[2], 10)) : size - 1;
  }
  if (start >= size || end < start) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
  }
  const body = Readable.toWeb(createReadStream(filePath, { start, end })) as ReadableStream<Uint8Array>;
  return new Response(body, {
    status: 206,
    headers: {
      ...headers,
      "Content-Length": String(end - start + 1),
      "Content-Range": `bytes ${start}-${end}/${size}`,
    },
  });
}

// Peak envelope for drawing. Supports the PCM widths ACE and exports produce.
export async function wavPeaks(filePath: string, targetBins = 1600): Promise<number[]> {
  let fileStat;
  try {
    fileStat = await stat(filePath);
  } catch {
    return [];
  }
  const cached = peaksCache.get(filePath);
  if (cached && cached.size === fileStat.size && cached.mtimeMs === fileStat.mtimeMs) return cached.peaks;
  const peaks = computePeaks(await readFile(filePath), targetBins);
  peaksCache.set(filePath, { size: fileStat.size, mtimeMs: fileStat.mtimeMs, peaks });
  return peaks;
}

function computePeaks(data: Buffer, targetBins: number): number[] {
  if (data.length < 44 || data.toString("ascii", 0, 4) !== "RIFF" || data.toString("ascii", 8, 12) !== "WAVE") {
    return [];
  }
  let channels = 0;
  let bitsPerSample = 0;
  let audioFormat = 0;
  let pcmStart = -1;
  let pcmBytes = 0;
  for (let offset = 12; offset + 8 <= data.length; ) {
    const chunkId = data.toString("ascii", offset, offset + 4);
    const chunkSize = data.readUInt32LE(offset + 4);
    const content = offset + 8;
    if (chunkId === "fmt " && chunkSize >= 16 && content + 16 <= data.length) {
      audioFormat = data.readUInt16LE(content);
      channels = data.readUInt16LE(content + 2);
      bitsPerSample = data.readUInt16LE(content + 14);
      if (audioFormat === 0xfffe && chunkSize >= 26) audioFormat = data.readUInt16LE(content + 24);
    } else if (chunkId === "data") {
      pcmStart = content;
      pcmBytes = Math.min(chunkSize, data.length - content);
      break;
    }
    offset = content + chunkSize + (chunkSize % 2);
  }
  const bytes = bitsPerSample / 8;
  const supported = (audioFormat === 1 && [16, 24].includes(bitsPerSample)) || (audioFormat === 3 && bitsPerSample === 32);
  if (!supported || channels < 1 || pcmStart < 0) return [];
  const frameBytes = channels * bytes;
  const frames = Math.floor(pcmBytes / frameBytes);
  if (!frames) return [];
  const bins = Math.max(1, Math.min(targetBins, frames));
  const framesPerBin = frames / bins;
  const peaks = new Array<number>(bins).fill(0);
  // Sampling a stride inside each bin keeps a 3-minute stereo file fast without losing peaks visibly.
  const stride = Math.max(1, Math.floor(framesPerBin / 256));
  const read = (offset: number): number => {
    if (audioFormat === 3) return Math.abs(data.readFloatLE(offset));
    if (bitsPerSample === 16) return Math.abs(data.readInt16LE(offset)) / 32768;
    return Math.abs(data.readIntLE(offset, 3)) / 8388608;
  };
  for (let bin = 0; bin < bins; bin += 1) {
    const first = Math.floor(bin * framesPerBin);
    const last = Math.min(frames, Math.floor((bin + 1) * framesPerBin));
    let peak = 0;
    for (let frame = first; frame < last; frame += stride) {
      const frameOffset = pcmStart + frame * frameBytes;
      for (let channel = 0; channel < channels; channel += 1) {
        const value = read(frameOffset + channel * bytes);
        if (value > peak) peak = value;
      }
    }
    peaks[bin] = Math.round(Math.min(1, peak) * 1000) / 1000;
  }
  return peaks;
}
