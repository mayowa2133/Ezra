"""Transcripts: word-level timestamps are the ground truth every clip boundary
is snapped to, so an agent's rough "start around 12:40" becomes a cut that
never lands mid-word.

On-disk format (JSON):
  {"language": "en", "duration": 4440.2,
   "segments": [{"start": 0.0, "end": 4.1, "text": "...",
                 "words": [{"w": "So", "s": 0.0, "e": 0.21}, ...]}]}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sources
from ..config import settings

LEAD_PAD = 0.25   # seconds kept before the first word
TAIL_PAD = 0.35   # seconds kept after the last word


@dataclass
class Word:
    w: str
    s: float
    e: float


@dataclass
class Transcript:
    language: str | None
    duration: float
    segments: list[dict[str, Any]]

    @property
    def words(self) -> list[Word]:
        return [Word(w["w"], float(w["s"]), float(w["e"]))
                for seg in self.segments for w in seg.get("words", [])]

    @classmethod
    def load(cls, path: str | Path) -> "Transcript":
        data = json.loads(Path(path).read_text())
        return cls(data.get("language"), float(data.get("duration") or 0), data.get("segments", []))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(
            {"language": self.language, "duration": self.duration, "segments": self.segments},
            ensure_ascii=False))


def fmt_ts(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m):02d}:{s:04.1f}"


def for_source(source_id: int) -> Transcript:
    src = sources.get(source_id)
    if not src.get("transcript_path"):
        raise RuntimeError(f"source {source_id} is not transcribed yet (status: {src['status']})")
    return Transcript.load(src["transcript_path"])


def transcript_path_for(source_id: int) -> Path:
    return settings().transcripts_dir / f"source-{source_id}.json"


def page(t: Transcript, start: float = 0.0, max_chars: int = 30000) -> dict[str, Any]:
    """Timestamped segment lines from `start`, cut at ~max_chars so an agent
    can read an hour-long episode in a few calls. `next_start` is None at the end."""
    lines: list[str] = []
    used = 0
    next_start = None
    for seg in t.segments:
        if seg["end"] <= start:
            continue
        line = f"[{fmt_ts(seg['start'])} → {fmt_ts(seg['end'])} | {seg['start']:.2f}s] {seg['text'].strip()}"
        if used + len(line) > max_chars and lines:
            next_start = float(seg["start"])
            break
        lines.append(line)
        used += len(line) + 1
    return {"language": t.language, "duration": t.duration, "from": start,
            "next_start": next_start, "text": "\n".join(lines)}


def join_words(tokens: list[str]) -> str:
    """Join ASR tokens, gluing pieces Whisper splits off ("$400" ",000", "40" "%")."""
    out = ""
    for tok in (t.strip() for t in tokens):
        if not tok:
            continue
        if out and (tok[0] in ",.!?;:%)'’" or (tok[0] == "-" and out[-1].isdigit())):
            out += tok
        else:
            out += (" " if out else "") + tok
    return out


def window_text(t: Transcript, start: float, end: float) -> str:
    return join_words([w.w for w in t.words if w.e > start and w.s < end])


def snap(t: Transcript, start: float, end: float) -> tuple[float, float]:
    """Move rough boundaries onto word edges, padded but never into the
    neighbouring word."""
    words = t.words
    if not words:
        return max(0.0, start), min(end, t.duration or end)
    inside = [i for i, w in enumerate(words) if w.e > start and w.s < end]
    if not inside:
        raise ValueError(f"no speech between {start:.2f}s and {end:.2f}s")
    first, last = inside[0], inside[-1]
    prev_end = words[first - 1].e if first > 0 else 0.0
    next_start = words[last + 1].s if last + 1 < len(words) else (t.duration or words[last].e + TAIL_PAD)
    s = max(prev_end, words[first].s - LEAD_PAD, 0.0)
    e = min(next_start, words[last].e + TAIL_PAD)
    if t.duration:
        e = min(e, t.duration)
    return round(s, 3), round(e, 3)
