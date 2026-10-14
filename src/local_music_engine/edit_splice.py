"""Keep everything outside a repainted range sample-identical to the source version.

ACE splices the source waveform back around a repainted range and then peak-normalizes
the whole file, so its result is the source times one gain everywhere except near the
edit. Measured on saved repaints, that gain was 0.8 to 1.0 dB and the difference left
after undoing it was quantization noise. This module measures the gain where the edit
cannot have reached, undoes it for the edited stretch only, and writes the source's own
samples everywhere else. An edit therefore no longer lowers the rest of the song, and
"the rest is unchanged" is something the file itself shows.

The new stretch keeps the loudness the engine gave it relative to the rest. Its few
peaks that rise above the source's own peak are brought under it by a look-ahead gain
curve a few milliseconds wide, so the edit needs no headroom the source did not have.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import minimum_filter1d
from scipy.signal import stft

VERSION = "edit-splice-v1"
_LOOKAHEAD_SECONDS = 0.0025       # each side of a peak
_MAXIMUM_REDUCTION_DB = 2.0       # a louder edit keeps the engine's own file instead
_REFERENCE_MARGIN_SECONDS = 3.0   # the gain is fitted no closer than this to the edit
_MINIMUM_REFERENCE_SECONDS = 1.0
_WINDOW_SECONDS = 0.01
_JOIN_SECONDS = 0.1               # unchanged stretch that ends the edited region
_TOLERANCE_LSB = 3.0
_MAXIMUM_RESIDUAL_DB = -40.0
_BLOCK_FRAMES = 1 << 18


def _read(path: Path) -> tuple[wave._wave_params, np.ndarray]:
    with wave.open(str(path), "rb") as audio:
        params = audio.getparams()
        if params.comptype != "NONE" or params.sampwidth != 2 or not 1 <= params.nchannels <= 2:
            raise ValueError("unsupported_format")
        data = np.frombuffer(audio.readframes(params.nframes), dtype="<i2")
    if len(data) != params.nframes * params.nchannels:
        raise ValueError("truncated_audio")
    return params, data.reshape(params.nframes, params.nchannels)


def _under_ceiling(samples: np.ndarray, ceiling: float, rate: int) -> tuple[np.ndarray, float, float]:
    """Lower only the peaks above ``ceiling``; returns the samples, the deepest reduction in dB and the seconds touched.

    Each sample's gain is the smallest gain any peak within the look-ahead needs, smoothed
    over the same width, so it is never higher than that sample needs and never jumps.
    """

    need = np.minimum(1.0, ceiling / np.maximum(np.abs(samples).max(axis=1), 1e-9))
    if not len(need) or need.min() >= 1.0:
        return samples, 0.0, 0.0
    half = max(1, round(_LOOKAHEAD_SECONDS * rate))
    width = 2 * half + 1
    held = minimum_filter1d(need, size=2 * width - 1, mode="nearest")
    kernel = np.hanning(width + 2)[1:-1]
    gain = np.convolve(np.pad(held, half, mode="edge"), kernel / kernel.sum(), mode="valid")
    # Both ends meet the source's own samples, which need no reduction: arrive there at unity.
    taper = np.minimum(1.0, np.minimum(np.arange(len(gain)), np.arange(len(gain))[::-1]) / width)
    gain = 1.0 - (1.0 - gain) * taper
    limited = np.clip(samples * gain[:, None], -ceiling, ceiling)
    return limited, -20 * math.log10(float(gain.min())), float((gain < 0.999).sum()) / rate


def _kept(reason: str, **detail: Any) -> dict[str, Any]:
    return {"version": VERSION, "applied": False, "reason": reason, **detail}


def restore_outside_edit(source: Path, edited: Path, *, start_seconds: float, end_seconds: float) -> dict[str, Any]:
    """Rewrite ``edited`` in place so only the stretch the engine changed differs from ``source``.

    ``edited`` must be the engine's freshly downloaded result, not yet recorded as an
    artifact. When the two files cannot be matched (other format, no stretch far from
    the edit, more than one gain, or an edit far louder than the source's own peak)
    the file is left exactly as the engine made it and the reason is returned.
    """

    try:
        source_params, original = _read(source)
        edited_params, engine = _read(edited)
    except (OSError, wave.Error, EOFError, ValueError) as error:
        return _kept(str(error) if isinstance(error, ValueError) else "unreadable_audio")
    rate, frames = source_params.framerate, source_params.nframes
    if (edited_params.framerate, edited_params.nchannels) != (rate, source_params.nchannels):
        return _kept("format_differs_from_source")
    if abs(edited_params.nframes - frames) > rate * 0.05:
        return _kept("length_differs_from_source")
    common = min(frames, edited_params.nframes)
    engine_sha256 = hashlib.sha256(edited.read_bytes()).hexdigest()
    first = max(0, min(common, round(start_seconds * rate)))
    last = max(first, min(common, round(end_seconds * rate)))
    margin = round(_REFERENCE_MARGIN_SECONDS * rate)
    far = [(0, max(0, first - margin)), (min(common, last + margin), common)]
    if sum(stop - begin for begin, stop in far) < _MINIMUM_REFERENCE_SECONDS * rate:
        return _kept("no_stretch_far_from_the_edit", engineOutputSha256=engine_sha256)

    cross = power = 0.0
    for begin, stop in far:
        for offset in range(begin, stop, _BLOCK_FRAMES):
            block = slice(offset, min(stop, offset + _BLOCK_FRAMES))
            reference = original[block].astype(np.float64)
            cross += float((engine[block] * reference).sum())
            power += float((reference * reference).sum())
    if power <= 0:
        return _kept("source_is_silent_outside_the_edit", engineOutputSha256=engine_sha256)
    gain = cross / power
    if not 0.25 <= gain <= 4.0:  # unrelated audio fits no gain at all
        return _kept("source_not_preserved_by_engine", gain=gain, engineOutputSha256=engine_sha256)

    # With the gain undone, what still differs from the source is what the engine changed.
    window = max(1, round(_WINDOW_SECONDS * rate))
    changed = np.zeros(math.ceil(common / window), dtype=bool)
    residual = 0.0
    for offset in range(0, common, _BLOCK_FRAMES):
        stop = min(common, offset + _BLOCK_FRAMES)
        difference = np.abs(engine[offset:stop] / gain - original[offset:stop]).max(axis=1)
        for begin, end in far:
            lo, hi = max(begin, offset), min(end, stop)
            if hi > lo:
                residual += float((difference[lo - offset:hi - offset] ** 2).sum())
        over = np.flatnonzero(difference > _TOLERANCE_LSB) + offset
        changed[np.unique(over // window)] = True
    far_frames = sum(stop - begin for begin, stop in far)
    residual_db = 10 * math.log10(max(residual / far_frames, 1e-12) / max(power / (far_frames * original.shape[1]), 1e-12))
    if residual_db > _MAXIMUM_RESIDUAL_DB:
        # The engine did more than scale the source out there; its file is kept as made.
        return _kept("source_not_preserved_by_engine", gain=gain, residualDb=residual_db, engineOutputSha256=engine_sha256)

    # Grow from the requested range through every changed window, stopping at the first
    # unchanged stretch long enough that both files agree there: the join is seamless.
    join = max(1, round(_JOIN_SECONDS / _WINDOW_SECONDS))
    low, high = first // window, max(first // window, (max(last, first + 1) - 1) // window)
    quiet = 0
    while low > 0 and quiet < join:
        low -= 1
        quiet = 0 if changed[low] else quiet + 1
    low += quiet
    quiet = 0
    while high < len(changed) - 1 and quiet < join:
        high += 1
        quiet = 0 if changed[high] else quiet + 1
    high -= quiet
    patch_start, patch_end = low * window, min(common, (high + 1) * window)
    stray = int(changed[:low].sum() + changed[high + 1:].sum())

    ceiling = float(np.abs(original.astype(np.int32)).max())
    patch = engine[patch_start:patch_end] / gain
    if patch.size and float(np.abs(patch).max()) > ceiling * 10 ** (_MAXIMUM_REDUCTION_DB / 20):
        return _kept("edit_much_louder_than_the_source", gain=gain, engineOutputSha256=engine_sha256)
    patch, reduction_db, limited_seconds = _under_ceiling(patch, ceiling, rate)
    result = original.copy()
    result[patch_start:patch_end] = np.rint(patch).astype("<i2")
    descriptor, name = tempfile.mkstemp(prefix=f".{edited.name}.", suffix=".splice", dir=edited.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as raw, wave.open(raw, "wb") as output:
            output.setparams(source_params)
            output.writeframes(result.tobytes())
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, edited)
    finally:
        temporary.unlink(missing_ok=True)
    return {"version": VERSION, "applied": True, "gain": gain, "gainDb": 20 * math.log10(gain), "residualDb": residual_db,
            "requestedRange": {"startSeconds": start_seconds, "endSeconds": end_seconds},
            "changedRange": {"startSeconds": patch_start / rate, "endSeconds": patch_end / rate},
            "strayChangedWindowsRestored": stray, "engineOutputSha256": engine_sha256,
            "peakCeiling": ceiling / 32768, "peakReductionDb": reduction_db, "peakReducedSeconds": limited_seconds,
            "method": "source samples outside the changed range; engine samples at the source's level inside it"}


_TEMPO_CONTEXT_SECONDS = 20.0
_TEMPO_SEARCH = 0.12            # the played tempo is looked for within ±12% of the requested one
_TEMPO_FOLLOW_ABOVE = 0.015     # smaller differences are left to the request


def played_tempo(source: Path, *, requested_bpm: float, start_seconds: float, end_seconds: float) -> dict[str, Any]:
    """The tempo the source actually plays at next to an edit, near the requested tempo.

    A song can drift from the BPM it was asked for (one measured at 87.5 for 84). A
    repaint conditioned on the request then keeps the requested tempo inside the edit and
    meets the song's own beat off the grid at the far end. This reads the onsets in up
    to 20 s on each side of the edit (never inside it): onset autocorrelation at one to
    four beats finds the beat period, and a line fitted through the onsets nearest each
    predicted beat refines it.
    """

    try:
        params, data = _read(source)
    except (OSError, wave.Error, EOFError, ValueError) as error:
        return {"status": "unknown", "reason": str(error) if isinstance(error, ValueError) else "unreadable_audio"}
    rate, hop = params.framerate, 0.005
    mono = data.astype(np.float64).mean(axis=1) / 32768
    requested_period = 60 / requested_bpm
    lags = np.arange(round(requested_period * (1 - _TEMPO_SEARCH) / hop), round(requested_period * (1 + _TEMPO_SEARCH) / hop) + 1)
    total = np.zeros(len(lags))
    envelopes = []
    for begin, stop in ((max(0.0, start_seconds - _TEMPO_CONTEXT_SECONDS), start_seconds),
                        (end_seconds, min(len(mono) / rate, end_seconds + _TEMPO_CONTEXT_SECONDS))):
        if stop - begin < 6 * requested_period:
            continue
        size, step = 2048, round(rate * hop)
        _, _, spectrum = stft(mono[round(begin * rate):round(stop * rate)], fs=rate, nperseg=size, noverlap=size - step,
                              boundary=None, padded=False)
        envelope = np.maximum(0, np.diff(np.log1p(100 * np.abs(spectrum)), axis=1)).sum(axis=0)
        envelopes.append((begin + (np.arange(len(envelope)) + 1) * hop + size / 2 / rate, envelope))
        centred = envelope - envelope.mean()
        correlation = np.correlate(centred, centred, mode="full")[len(centred) - 1:]
        # A beat period also lines up at two, three and four beats; summing those sharpens it.
        for multiple in (1, 2, 3, 4):
            positions = lags * multiple
            usable = positions < len(correlation)
            total[usable] += correlation[positions[usable]] / max(1.0, correlation[0])
    if not envelopes:
        return {"status": "unknown", "reason": "too_little_audio_next_to_the_edit"}
    peak = int(np.argmax(total))
    contrast = float(total[peak] / (np.abs(total).mean() + 1e-9))
    period = lags[peak] * hop
    # Refine: on each side, the strongest onset near every predicted beat, then one shared slope.
    indexes, times, weights = [], [], []
    for side, (clock, envelope) in enumerate(envelopes):
        phases = np.arange(0, period, hop)
        grid = [np.interp(np.arange(phase + clock[0], clock[-1], period), clock, envelope).sum() for phase in phases]
        origin = phases[int(np.argmax(grid))] + clock[0]
        for beat in range(int((clock[-1] - origin) / period) + 1):
            near = np.abs(clock - (origin + beat * period)) <= 0.06 * period
            if near.any() and envelope[near].max() > 0:
                position = int(np.flatnonzero(near)[np.argmax(envelope[near])])
                indexes.append((side, beat))
                times.append(clock[position])
                weights.append(envelope[position])
    if len(times) >= 8:
        sides = np.array([side for side, _ in indexes])
        design = np.column_stack([np.array([beat for _, beat in indexes], dtype=float)] + [(sides == side).astype(float) for side in range(len(envelopes))])
        root = np.sqrt(np.array(weights))
        period = float(np.linalg.lstsq(design * root[:, None], np.array(times) * root, rcond=None)[0][0])
    bpm = 60 / period
    edge = peak in (0, len(total) - 1)
    follow = bool(abs(bpm - requested_bpm) / requested_bpm >= _TEMPO_FOLLOW_ABOVE and contrast >= 1.5 and not edge)
    return {"status": "measured", "requestedBpm": requested_bpm, "playedBpm": round(float(bpm), 2), "contrast": round(contrast, 2),
            "measuredSeconds": round(float(sum(clock[-1] - clock[0] for clock, _ in envelopes)), 1), "follow": follow,
            "method": "onset autocorrelation at 1-4 beats, refined through the onsets on each side of the edit"}
