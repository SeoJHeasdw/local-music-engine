"""The tempo a song actually plays at, measured from its own onsets.

A generated song can play at a different tempo from the BPM it was asked for: in the saved
ACE takes, 84 BPM requests played at 84.0–87.6, 98 at 88.2–96.8 and 80 at 88.3, while 116 and
125 stayed within 1.6%. Anything that must meet the song's own beat (a repaint, an extension)
has to use what was played, not what was asked.

The beat is looked for within ±12% of the tempo the song was conditioned on, so the beat
unit is the one the request named (not a half- or double-time alias of it). Inside that
window everything is measured: onset autocorrelation at one to four beats finds the period,
and a line fitted through the strongest onset near every predicted beat refines it.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import stft

VERSION = "played-tempo-v1"
_HOP_SECONDS = 0.005
_FFT_SIZE = 2048
_SEARCH = 0.12               # the played tempo is looked for within ±12% of the reference
_MINIMUM_CONTRAST = 1.5      # weaker periodicity is not a beat to follow
_EDIT_CONTEXT_SECONDS = 20.0
_FOLLOW_ABOVE = 0.015        # a repaint keeps the request when the song plays this close to it


def _mono(source: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(source), "rb") as audio:
        params = audio.getparams()
        if params.comptype != "NONE" or params.sampwidth != 2 or not 1 <= params.nchannels <= 2:
            raise ValueError("unsupported_format")
        data = np.frombuffer(audio.readframes(params.nframes), dtype="<i2")
    if len(data) != params.nframes * params.nchannels:
        raise ValueError("truncated_audio")
    return params.framerate, data.reshape(params.nframes, params.nchannels).astype(np.float64).mean(axis=1) / 32768


def _measure(source: Path, reference_bpm: float, spans: list[tuple[float, float]]) -> dict[str, Any]:
    try:
        rate, mono = _mono(source)
    except (OSError, wave.Error, EOFError, ValueError) as error:
        return {"status": "unknown", "reason": str(error) if isinstance(error, ValueError) else "unreadable_audio"}
    hop = _HOP_SECONDS
    reference_period = 60 / reference_bpm
    lags = np.arange(round(reference_period * (1 - _SEARCH) / hop), round(reference_period * (1 + _SEARCH) / hop) + 1)
    total = np.zeros(len(lags))
    envelopes = []
    for begin, stop in spans:
        stop = min(stop, len(mono) / rate)
        if stop - begin < 6 * reference_period:
            continue
        step = round(rate * hop)
        _, _, spectrum = stft(mono[round(begin * rate):round(stop * rate)], fs=rate, nperseg=_FFT_SIZE,
                              noverlap=_FFT_SIZE - step, boundary=None, padded=False)
        envelope = np.maximum(0, np.diff(np.log1p(100 * np.abs(spectrum)), axis=1)).sum(axis=0)
        envelopes.append((begin + (np.arange(len(envelope)) + 1) * hop + _FFT_SIZE / 2 / rate, envelope))
        centred = envelope - envelope.mean()
        energy = max(1.0, float(np.dot(centred, centred)))
        # A beat period also lines up at two, three and four beats; summing those sharpens it.
        for multiple in (1, 2, 3, 4):
            for index, lag in enumerate(lags * multiple):
                if lag < len(centred):
                    total[index] += float(np.dot(centred[lag:], centred[:-lag])) / energy
    if not envelopes:
        return {"status": "unknown", "reason": "too_little_audio"}
    peak = int(np.argmax(total))
    contrast = float(total[peak] / (np.abs(total).mean() + 1e-9))
    period = lags[peak] * hop
    # Refine: in each span, the strongest onset near every predicted beat, then one shared slope.
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
        design = np.column_stack([np.array([beat for _, beat in indexes], dtype=float)]
                                 + [(sides == side).astype(float) for side in range(len(envelopes))])
        root = np.sqrt(np.array(weights))
        period = float(np.linalg.lstsq(design * root[:, None], np.array(times) * root, rcond=None)[0][0])
    return {"status": "measured", "playedBpm": round(float(60 / period), 2), "contrast": round(contrast, 2),
            "atSearchEdge": peak in (0, len(total) - 1),
            "measuredSeconds": round(float(sum(clock[-1] - clock[0] for clock, _ in envelopes)), 1)}


def played_tempo(source: Path, *, requested_bpm: float, start_seconds: float, end_seconds: float) -> dict[str, Any]:
    """The tempo next to an edit: up to 20 s on each side, never inside the stretch being replaced.

    A repaint conditioned on the request keeps the requested tempo inside the edit and meets
    the song's own beat off the grid at the far end, so ``follow`` says to ask for this one.
    """

    measured = _measure(source, requested_bpm, [(max(0.0, start_seconds - _EDIT_CONTEXT_SECONDS), start_seconds),
                                                (end_seconds, end_seconds + _EDIT_CONTEXT_SECONDS)])
    if measured["status"] != "measured":
        return {**measured, "reason": "too_little_audio_next_to_the_edit"} if measured["reason"] == "too_little_audio" else measured
    bpm = measured["playedBpm"]
    follow = bool(abs(bpm - requested_bpm) / requested_bpm >= _FOLLOW_ABOVE and measured["contrast"] >= _MINIMUM_CONTRAST
                  and not measured["atSearchEdge"])
    return {"status": "measured", "requestedBpm": requested_bpm, "playedBpm": bpm, "contrast": measured["contrast"],
            "measuredSeconds": measured["measuredSeconds"], "follow": follow,
            "method": "onset autocorrelation at 1-4 beats, refined through the onsets on each side of the edit"}


def song_tempo(source: Path, *, reference_bpm: float | None, reference_source: str) -> dict[str, Any]:
    """The tempo the whole song plays at, for every step that must meet its beat.

    ``reference_bpm`` is the tempo the song was conditioned on (the request, or the engine's
    own choice when none was requested). Weak periodicity or a peak on the edge of the search
    window is reported as unknown rather than as a tempo.
    """

    result: dict[str, Any] = {"version": VERSION, "referenceBpm": reference_bpm, "referenceSource": reference_source,
                              "searchPercent": round(_SEARCH * 100)}
    if reference_bpm is None:
        return {**result, "status": "unknown", "reason": "no_tempo_to_search_near"}
    measured = _measure(source, float(reference_bpm), [(0.0, float("inf"))])
    if measured["status"] != "measured":
        return {**result, **measured}
    if measured["contrast"] < _MINIMUM_CONTRAST:
        return {**result, "status": "unknown", "reason": "no_clear_beat", "contrast": measured["contrast"]}
    if measured["atSearchEdge"]:
        return {**result, "status": "unknown", "reason": "beat_outside_the_search_window", "contrast": measured["contrast"]}
    bpm = measured["playedBpm"]
    return {**result, "status": "measured", "playedBpm": bpm,
            "differencePercent": round((bpm - reference_bpm) / reference_bpm * 100, 2),
            "contrast": measured["contrast"], "measuredSeconds": measured["measuredSeconds"],
            "method": "onset autocorrelation at 1-4 beats over the whole song, refined through the onset nearest every beat"}
