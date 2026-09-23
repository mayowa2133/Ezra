"""Speaker diarization: who speaks when.

Providers
  local     (default) MFCC voice statistics over word-aligned windows, clustered
            with average-linkage cosine distance; the speaker count is chosen by
            silhouette score. No model download, no credentials. It is a
            heuristic: good at separating clearly different voices (host/guest
            of different sex or accent), weak on similar voices and overlap.
            Its confidence (silhouette) is stored with the result.
  pyannote  pyannote.audio (MIT) with the gated `speaker-diarization-community-1`
            pipeline: accept its terms on Hugging Face and set HF_TOKEN.
  none      no speaker labels.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import secrets
from .config import get_settings
from .transcription.base import Word

SR = 16000


@dataclass
class Turn:
    start: float
    end: float
    speaker: str


@dataclass
class Diarization:
    turns: list[Turn]
    n_speakers: int
    confidence: float | None
    provider: str


class Diarizer(ABC):
    name: str

    @abstractmethod
    def diarize(self, media: Path, words: list[Word]) -> Diarization: ...


def load_audio(media: Path, sr: int = SR) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(media), "-ac", "1", "-ar", str(sr),
                          "-f", "s16le", "-"], capture_output=True, check=True)
    return np.frombuffer(out.stdout, np.int16).astype(np.float32) / 32768.0


# --- MFCC (numpy) --------------------------------------------------------------

def _mel_filterbank(n_mels: int, n_fft: int, sr: int) -> np.ndarray:
    def hz_to_mel(f: np.ndarray) -> np.ndarray:
        return 2595 * np.log10(1 + f / 700)

    def mel_to_hz(m: np.ndarray) -> np.ndarray:
        return 700 * (10 ** (m / 2595) - 1)

    mels = np.linspace(hz_to_mel(np.array(60.0)), hz_to_mel(np.array(sr / 2 - 200.0)), n_mels + 2)
    bins = np.floor((n_fft + 1) * mel_to_hz(mels) / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1))
    for i in range(1, n_mels + 1):
        lo, mid, hi = bins[i - 1], bins[i], bins[i + 1]
        if mid > lo:
            fb[i - 1, lo:mid] = (np.arange(lo, mid) - lo) / (mid - lo)
        if hi > mid:
            fb[i - 1, mid:hi] = (hi - np.arange(mid, hi)) / (hi - mid)
    return fb


def mfcc(signal: np.ndarray, sr: int = SR, n_mfcc: int = 20, n_mels: int = 40) -> np.ndarray:
    n_fft, hop, win = 512, int(0.010 * sr), int(0.025 * sr)
    if len(signal) < win:
        return np.zeros((0, n_mfcc))
    emph = np.append(signal[0], signal[1:] - 0.97 * signal[:-1])
    n_frames = 1 + (len(emph) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = emph[idx] * np.hamming(win)
    power = np.abs(np.fft.rfft(frames, n_fft)) ** 2 / n_fft
    energy = np.log(np.maximum(power @ _mel_filterbank(n_mels, n_fft, sr).T, 1e-10))
    from scipy.fft import dct

    return dct(energy, type=2, axis=1, norm="ortho")[:, 1:n_mfcc + 1]


# --- clustering ----------------------------------------------------------------

def _silhouette(X: np.ndarray, labels: np.ndarray) -> float:
    from scipy.spatial.distance import cdist

    ks = np.unique(labels)
    if len(ks) < 2:
        return -1.0
    D = cdist(X, X, "cosine")
    scores = []
    for i in range(len(X)):
        same = labels == labels[i]
        same[i] = False
        if not same.any():
            scores.append(0.0)
            continue
        a = D[i, same].mean()
        b = min(D[i, labels == k].mean() for k in ks if k != labels[i])
        scores.append((b - a) / max(a, b) if max(a, b) > 0 else 0.0)
    return float(np.mean(scores))


STRONG_SILHOUETTE = 0.25    # a split this clean is real on its own
WEAK_SILHOUETTE = 0.15      # a weaker split needs a clear jump in the final merge too
MERGE_JUMP = 1.2


def cluster(X: np.ndarray, max_speakers: int = 6) -> tuple[np.ndarray, float]:
    """Average-linkage clustering of speech-window embeddings; the number of
    speakers is the k with the best silhouette, but only when the split is real.
    One voice still splits into "clusters" (intonation, loudness) with a
    silhouette around 0.15 and no jump in merge height, while distinct voices
    give about 0.4 and a final merge well above the rest (measured on the
    benchmark fixtures), so a single speaker is the default."""
    from scipy.cluster.hierarchy import fcluster, linkage

    if len(X) < 4:
        return np.zeros(len(X), dtype=int), 1.0
    Z = linkage(X, method="average", metric="cosine")
    heights = Z[:, 2]
    jump = float(heights[-1] / heights[-2]) if len(heights) > 1 and heights[-2] > 0 else 1.0
    min_silhouette = STRONG_SILHOUETTE if jump < MERGE_JUMP else WEAK_SILHOUETTE
    best_k, best_s, best = 1, min_silhouette, np.zeros(len(X), dtype=int)
    for k in range(2, min(max_speakers, len(X) - 1) + 1):
        labels = fcluster(Z, k, criterion="maxclust") - 1
        # ignore splits that carve off a handful of windows (usually noise)
        if np.bincount(labels).min() < max(2, int(0.03 * len(X))):
            continue
        sc = _silhouette(X, labels)
        if sc > best_s:
            best_k, best_s, best = k, sc, labels
    return best, (best_s if best_k > 1 else 1.0)


class LocalDiarizer(Diarizer):
    name = "local"
    WINDOW = 1.6   # seconds of speech per embedding

    def diarize(self, media: Path, words: list[Word]) -> Diarization:
        if not words:
            return Diarization([], 0, None, self.name)
        audio = load_audio(media)
        # word-aligned windows: consecutive words until ~WINDOW seconds, split at pauses
        windows: list[list[Word]] = []
        cur: list[Word] = []
        for w in words:
            if cur and (w.e - cur[0].s > self.WINDOW or w.s - cur[-1].e > 0.5):
                windows.append(cur)
                cur = []
            cur.append(w)
        if cur:
            windows.append(cur)
        feats, keep = [], []
        for i, win in enumerate(windows):
            a, b = int(win[0].s * SR), int(win[-1].e * SR)
            m = mfcc(audio[a:b])
            if len(m) < 20:
                continue
            feats.append(np.concatenate([m.mean(0), m.std(0)]))
            keep.append(i)
        if not feats:
            return Diarization([Turn(words[0].s, words[-1].e, "S1")], 1, None, self.name)
        X = np.array(feats)
        X = (X - X.mean(0)) / (X.std(0) + 1e-8)
        labels, conf = cluster(X)
        # windows too short to embed inherit their neighbour's label
        full = np.full(len(windows), -1)
        full[keep] = labels
        for i in range(len(full)):
            if full[i] < 0:
                full[i] = full[i - 1] if i > 0 and full[i - 1] >= 0 else (labels[0] if len(labels) else 0)
        # smooth single-window flips (A B A -> A A A)
        for i in range(1, len(full) - 1):
            if full[i - 1] == full[i + 1] != full[i]:
                full[i] = full[i - 1]
        order: dict[int, str] = {}
        turns: list[Turn] = []
        for win, lab in zip(windows, full):
            spk = order.setdefault(int(lab), f"S{len(order) + 1}")
            if turns and turns[-1].speaker == spk:
                turns[-1].end = win[-1].e
            else:
                turns.append(Turn(win[0].s, win[-1].e, spk))
        return Diarization(turns, len(order), round(conf, 3), self.name)


class PyannoteDiarizer(Diarizer):
    name = "pyannote"
    PIPELINE = "pyannote/speaker-diarization-community-1"

    def diarize(self, media: Path, words: list[Word]) -> Diarization:
        token = secrets.require("huggingface", "pyannote diarization (accept the model terms on HF first)")
        try:
            from pyannote.audio import Pipeline
        except ImportError as e:
            raise RuntimeError("pyannote.audio is not installed: `uv pip install pyannote.audio`") from e
        wav = get_settings().work_dir / f"diar-{media.stem}.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(media), "-ac", "1", "-ar", str(SR), str(wav)],
                       check=True)
        pipeline = Pipeline.from_pretrained(self.PIPELINE, token=token)
        out = pipeline(str(wav))
        annotation = getattr(out, "speaker_diarization", out)
        turns: list[Turn] = []
        names: dict[str, str] = {}
        for turn, _track, label in annotation.itertracks(yield_label=True):
            spk = names.setdefault(label, f"S{len(names) + 1}")
            turns.append(Turn(float(turn.start), float(turn.end), spk))
        wav.unlink(missing_ok=True)
        return Diarization(turns, len(names), None, self.name)


class NoDiarizer(Diarizer):
    name = "none"

    def diarize(self, media: Path, words: list[Word]) -> Diarization:
        return Diarization([], 0, None, self.name)


DIARIZERS: dict[str, type[Diarizer]] = {"local": LocalDiarizer, "pyannote": PyannoteDiarizer, "none": NoDiarizer}


def get_diarizer(name: str | None = None) -> Diarizer:
    name = name or get_settings().diarizer
    if name not in DIARIZERS:
        raise ValueError(f"unknown diarizer {name!r}; available: {sorted(DIARIZERS)}")
    return DIARIZERS[name]()


def assign_speakers(words: list[Word], turns: list[Turn]) -> None:
    """Label each word with the turn it overlaps most."""
    if not turns:
        return
    j = 0
    for w in words:
        while j + 1 < len(turns) and turns[j].end < w.s:
            j += 1
        best, best_ov = None, 0.0
        for t in turns[max(0, j - 1): j + 2]:
            ov = min(w.e, t.end) - max(w.s, t.start)
            if ov > best_ov:
                best, best_ov = t, ov
        if best is None:  # word in a gap: nearest turn
            best = min(turns[max(0, j - 1): j + 2], key=lambda t: min(abs(t.start - w.s), abs(t.end - w.e)))
        w.spk = best.speaker
