import type { Song, TimeRange, Version } from "../shared.ts";
import { versionSourceParent } from "./format.ts";
import { isAutomaticAttempt } from "./quality.ts";

// Comparison is playback only. It never supplies a waveform to generation or
// changes the canonical recommendation, final selection, or listening review.
export function comparisonChoices(song: Song, current: Version): Version[] {
  const parent = versionSourceParent(song, current);
  return song.versions.filter((version) => version.id !== current.id && version.fileOk && version.audioUrl
    && (!isAutomaticAttempt(version) || version.id === parent?.id));
}

export function comparisonVersion(song: Song, current: Version, chosenId: string | null | undefined): Version | undefined {
  const choices = comparisonChoices(song, current);
  if (chosenId) return choices.find((version) => version.id === chosenId);
  const parent = versionSourceParent(song, current);
  return choices.find((version) => version.id === parent?.id);
}

// A selected interval can outlast a shorter comparison take. Loop only its
// audible overlap; an interval beyond the end must not seek forever at EOF.
export function audibleSelection(range: TimeRange | null, duration: number): TimeRange | null {
  if (!range || !Number.isFinite(duration) || duration <= 0) return null;
  const startSeconds = Math.max(0, range.startSeconds);
  const endSeconds = Math.min(duration, range.endSeconds);
  return Number.isFinite(startSeconds) && Number.isFinite(endSeconds) && endSeconds - startSeconds >= 0.05
    ? { startSeconds, endSeconds } : null;
}
