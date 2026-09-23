"""Transcript data model shared by every provider, plus the text utilities the
rest of Ezra relies on (sentence segmentation, word-boundary snapping)."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

Progress = Callable[[float], None]


@dataclass
class Word:
    w: str
    s: float
    e: float
    p: float | None = None          # probability / confidence
    spk: str | None = None          # speaker label

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Word":
        return cls(w=d["w"], s=float(d["s"]), e=float(d["e"]), p=d.get("p"), spk=d.get("spk"))


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    speaker: str | None = None
    confidence: float | None = None
    sentence_end: bool = True


@dataclass
class TranscriptResult:
    language: str | None
    duration: float
    segments: list[Segment]
    provider: str
    model: str | None

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]


class TranscriptionProvider(ABC):
    name: str

    @abstractmethod
    def settings(self) -> dict[str, Any]:
        """Everything that changes the output; hashed into the cache key."""

    @abstractmethod
    def transcribe(self, media: Path, progress: Progress | None = None) -> TranscriptResult: ...

    def version(self) -> str:
        blob = json.dumps({"provider": self.name, **self.settings()}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --- text utilities ------------------------------------------------------------

GLUE = ",.!?;:%)'’"
SENTENCE_END = (".", "?", "!")


def join_words(tokens: list[str]) -> str:
    """Join ASR tokens, gluing pieces Whisper splits off ("$400" ",000", "40" "%")."""
    out = ""
    for tok in (t.strip() for t in tokens):
        if not tok:
            continue
        if out and (tok[0] in GLUE or (tok[0] == "-" and out[-1].isdigit())):
            out += tok
        else:
            out += (" " if out else "") + tok
    return out


def ends_sentence(word: str) -> bool:
    return word.rstrip("\"')”’").endswith(SENTENCE_END)


def resegment(words: list[Word], max_span: float = 12.0, pause: float = 0.8) -> list[Segment]:
    """Sentence-sized segments from word timings: split at sentence punctuation,
    long pauses, speaker changes, or max_span seconds. Agents and the UI read one
    timestamp per segment, so batched ASR's ~30s segments are too coarse."""
    segments: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        speaker_change = bool(w.spk and cur and cur[-1].spk and w.spk != cur[-1].spk)
        # A speaker change splits only at a pause: diarization often mislabels the
        # first or last word of a turn, and a split there cuts a sentence in half.
        if cur and (w.s - cur[-1].e > pause or w.e - cur[0].s > max_span
                    or (speaker_change and w.s - cur[-1].e > 0.3)):
            segments.append(cur)
            cur = []
        cur.append(w)
        if ends_sentence(w.w):
            segments.append(cur)
            cur = []
    if cur:
        segments.append(cur)
    out = []
    for seg in segments:
        probs = [w.p for w in seg if w.p is not None]
        speakers = [w.spk for w in seg if w.spk]
        out.append(Segment(start=seg[0].s, end=seg[-1].e, text=join_words([w.w for w in seg]), words=seg,
                           speaker=max(set(speakers), key=speakers.count) if speakers else None,
                           confidence=round(sum(probs) / len(probs), 3) if probs else None,
                           sentence_end=ends_sentence(seg[-1].w)))
    return out


LEAD_PAD = 0.25
TAIL_PAD = 0.35


def window(words: list[Word], start: float, end: float) -> list[Word]:
    return [w for w in words if w.e > start and w.s < end]


def window_text(words: list[Word], start: float, end: float) -> str:
    return join_words([w.w for w in window(words, start, end)])


def snap(words: list[Word], start: float, end: float, duration: float | None = None) -> tuple[float, float]:
    """Move rough boundaries onto word edges, padded but never into the
    neighbouring word."""
    if not words:
        return max(0.0, start), min(end, duration or end)
    inside = [i for i, w in enumerate(words) if w.e > start and w.s < end]
    if not inside:
        raise ValueError(f"no speech between {start:.2f}s and {end:.2f}s")
    first, last = inside[0], inside[-1]
    prev_end = words[first - 1].e if first > 0 else 0.0
    next_start = words[last + 1].s if last + 1 < len(words) else ((duration or words[last].e) + TAIL_PAD)
    s = max(prev_end, words[first].s - LEAD_PAD, 0.0)
    e = min(next_start, words[last].e + TAIL_PAD)
    if duration:
        e = min(e, duration)
    return round(s, 3), round(e, 3)


def edges(words: list[Word], n: int = 12) -> dict[str, Any]:
    """Opening and closing words of a cut, with a warning for mid-sentence endings."""
    text = join_words([w.w for w in words]).split()
    warnings = []
    if words and not ends_sentence(words[-1].w):
        warnings.append("ends mid-sentence: extend `end` to the end of the sentence")
    return {"opens_with": " ".join(text[:n]), "ends_with": " ".join(text[-n:]), "warnings": warnings}
