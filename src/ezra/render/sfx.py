"""Synthesised transition sounds for clips (no samples, nothing licensed): a soft whoosh under each
cut and zoom change, the way short-form editors mark a jump. Kept well under the voice."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

RATE = 48000
WHOOSH_DB = -24.0     # peak level under a voice normalised to about -14 LUFS
MIN_GAP = 1.2         # seconds between two whooshes; closer ones are dropped
EDGE = 0.4            # no whoosh this close to the start or end of the clip


def whoosh(dur: float = 0.4, seed: int = 0) -> np.ndarray:
    """Band-limited noise swelling in and out over `dur` seconds (mono, peak 1)."""
    rng = np.random.default_rng(seed)
    n = int(dur * RATE)
    tt = np.arange(n) / RATE
    env = np.sin(np.pi * tt / dur) ** 2
    k = np.exp(-np.arange(60) / 12.0)                    # gentle lowpass: air, not hiss
    sig = np.convolve(rng.normal(0, 1, n), k / k.sum(), "same") * env
    return sig / max(1e-9, float(np.abs(sig).max()))


def spaced(times: list[float], duration: float) -> list[float]:
    """The times worth a whoosh: inside the clip, away from its edges, at least MIN_GAP apart."""
    out: list[float] = []
    for t in sorted(times):
        if EDGE <= t <= duration - EDGE and (not out or t - out[-1] >= MIN_GAP):
            out.append(t)
    return out


def track(times: list[float], duration: float, path: Path, db: float = WHOOSH_DB) -> Path:
    """A stereo WAV as long as the clip with a whoosh centred on each time."""
    out = np.zeros(int(duration * RATE) + 1)
    gain = 10 ** (db / 20)
    for i, t in enumerate(times):
        sig = whoosh(seed=i) * gain
        a = int(max(0.0, t - len(sig) / RATE / 2) * RATE)
        n = min(len(sig), len(out) - a)
        if n > 0:
            out[a:a + n] += sig[:n]
    pcm = (np.clip(out, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())
    return path
