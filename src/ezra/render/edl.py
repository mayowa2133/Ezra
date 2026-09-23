"""Edit decision list: which ranges of the source survive silence and
filler-word removal, and how source time maps to output time.

Captions, face tracking and B-roll all operate on the *output* timeline, so
every word is remapped through the EDL before rendering."""

from __future__ import annotations

from dataclasses import dataclass

from ..transcription.base import Word

FILLERS = {"um", "uh", "erm", "er", "uhm", "hmm", "mm", "ah"}
KEEP_PAUSE = 0.18     # silence kept where a long pause is shortened (reads as a cut, not a jump)
MIN_PIECE = 0.12      # drop slivers shorter than this


@dataclass
class Piece:
    src_start: float
    src_end: float

    @property
    def length(self) -> float:
        return self.src_end - self.src_start


def is_filler(word: str) -> bool:
    return word.lower().strip(" ,.!?;:-") in FILLERS


def build(words: list[Word], start: float, end: float, remove_silence: bool, silence_threshold: float,
          remove_fillers: bool) -> tuple[list[Piece], dict[str, float]]:
    """Keep ranges inside [start, end]. Long gaps between words shrink to
    KEEP_PAUSE; filler words (and the gap around them) are cut."""
    inside = [w for w in words if w.e > start and w.s < end]
    if not (remove_silence or remove_fillers) or not inside:
        return [Piece(start, end)], {"removed_silence": 0.0, "removed_fillers": 0, "removed_seconds": 0.0}
    cuts: list[tuple[float, float]] = []
    removed_fillers = 0
    removed_silence = 0.0
    prev_end = start
    for i, w in enumerate(inside):
        if remove_fillers and is_filler(w.w):
            nxt = inside[i + 1].s if i + 1 < len(inside) else min(end, w.e + 0.1)
            cuts.append((max(prev_end, w.s - 0.02), min(nxt, w.e + 0.05)))
            removed_fillers += 1
            continue
        gap = w.s - prev_end
        if remove_silence and gap > silence_threshold:
            half = KEEP_PAUSE / 2
            cuts.append((prev_end + half, w.s - half))
            removed_silence += gap - KEEP_PAUSE
        prev_end = w.e
    tail = end - prev_end
    if remove_silence and tail > silence_threshold:
        cuts.append((prev_end + KEEP_PAUSE, end))
        removed_silence += tail - KEEP_PAUSE
    pieces: list[Piece] = []
    cursor = start
    for a, b in sorted(cuts):
        if b <= a:
            continue
        if a - cursor >= MIN_PIECE:
            pieces.append(Piece(cursor, a))
        cursor = max(cursor, b)
    if end - cursor >= MIN_PIECE:
        pieces.append(Piece(cursor, end))
    if not pieces:
        pieces = [Piece(start, end)]
    total = sum(p.length for p in pieces)
    return pieces, {"removed_silence": round(removed_silence, 2), "removed_fillers": removed_fillers,
                    "removed_seconds": round((end - start) - total, 2)}


def map_time(pieces: list[Piece], t: float) -> float | None:
    """Source time → output time; None if t falls in a removed range."""
    out = 0.0
    for p in pieces:
        if p.src_start <= t <= p.src_end:
            return out + (t - p.src_start)
        if t < p.src_start:
            return None
        out += p.length
    return None


def remap_words(words: list[Word], pieces: list[Piece]) -> list[Word]:
    """Words that survive the edit, with output-timeline timestamps."""
    out = []
    for w in words:
        if is_filler(w.w):
            continue
        s = map_time(pieces, w.s)
        e = map_time(pieces, w.e)
        if s is None and e is None:
            continue
        if s is None:
            s = max(0.0, (e or 0) - (w.e - w.s))
        if e is None:
            e = s + (w.e - w.s)
        out.append(Word(w.w, round(s, 3), round(e, 3), w.p, w.spk))
    return out


def output_duration(pieces: list[Piece]) -> float:
    return sum(p.length for p in pieces)
