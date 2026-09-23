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


def _quiet_parts(a: float, b: float, quiet: list[tuple[float, float]] | None) -> list[tuple[float, float]]:
    """The parts of the gap [a, b] where the audio is actually quiet. Without
    silence data, the whole gap counts (fine for talk; wrong under music)."""
    if quiet is None:
        return [(a, b)]
    return [(max(a, qa), min(b, qb)) for qa, qb in quiet if qb > a and qa < b]


def build(words: list[Word], start: float, end: float, remove_silence: bool, silence_threshold: float,
          remove_fillers: bool, quiet: list[tuple[float, float]] | None = None) -> tuple[list[Piece], dict[str, float]]:
    """Keep ranges inside [start, end]. Long *quiet* gaps between words shrink to
    KEEP_PAUSE; filler words (and the gap around them) are cut. `quiet` is the
    source's detected silence: a gap full of music, sound effects or action has
    no words but isn't dead air, and must stay."""
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
        if remove_silence and w.s - prev_end > silence_threshold:
            half = KEEP_PAUSE / 2
            for qa, qb in _quiet_parts(prev_end, w.s, quiet):
                if qb - qa > silence_threshold:
                    cuts.append((qa + half, qb - half))
                    removed_silence += qb - qa - KEEP_PAUSE
        prev_end = w.e
    if remove_silence and end - prev_end > silence_threshold:
        for qa, qb in _quiet_parts(prev_end, end, quiet):
            if qb - qa > silence_threshold:
                lead = KEEP_PAUSE if qa <= prev_end + 1e-6 else KEEP_PAUSE / 2
                cuts.append((qa + lead, qb if qb >= end - 1e-6 else qb - KEEP_PAUSE / 2))
                removed_silence += qb - qa - KEEP_PAUSE
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


SEARCH = 0.12         # seconds either side of a cut searched for a quieter point
FRAME = 0.005         # energy frame length (see compose.audio_energy)


def refine_boundaries(pieces: list[Piece], words: list[Word], times: list[float],
                      energy: list[float]) -> tuple[list[Piece], int]:
    """Move each cut to the quietest audio frame within SEARCH of it, without
    entering a kept word. Word timestamps from ASR are +/-50-100 ms, so a cut
    placed on them can keep the tail of the previous word (an audible blip) or
    clip the next one; the waveform knows better. Returns (pieces, moved)."""
    if not times or not energy:
        return pieces, 0
    kept = [w for w in words if not is_filler(w.w)]

    def quietest(lo: float, hi: float, target: float) -> float:
        idx = [i for i, t in enumerate(times) if lo <= t <= hi]
        if not idx:
            return target
        at = min(range(len(times)), key=lambda i: abs(times[i] - target))
        best = min(idx, key=lambda i: (energy[i], abs(times[i] - target)))
        # only move for a real dip: stay put when the boundary is already quiet
        return times[best] if energy[best] < 0.5 * energy[at] else target

    out: list[Piece] = []
    moved = 0
    for i, p in enumerate(pieces):
        inside = [w for w in kept if w.e > p.src_start and w.s < p.src_end]
        floor = out[-1].src_end if out else p.src_start - SEARCH
        ceil = pieces[i + 1].src_start if i + 1 < len(pieces) else p.src_end + SEARCH
        lo_s, hi_s = max(p.src_start - SEARCH, floor), p.src_start + SEARCH
        lo_e, hi_e = p.src_end - SEARCH, min(p.src_end + SEARCH, ceil)
        if inside:
            hi_s = min(hi_s, inside[0].s)
            lo_e = max(lo_e, inside[-1].e)
            before = [w for w in words if w.e <= inside[0].s and w is not inside[0]]
            after = [w for w in words if w.s >= inside[-1].e and w is not inside[-1]]
            if before:   # may move later into the gap, never back into the previous word
                lo_s = max(lo_s, min(before[-1].e, p.src_start))
            if after:
                hi_e = min(hi_e, max(after[0].s, p.src_end))
        s = quietest(lo_s, hi_s, p.src_start) if lo_s < hi_s else p.src_start
        e = quietest(lo_e, hi_e, p.src_end) if lo_e < hi_e else p.src_end
        if e - s < MIN_PIECE:
            s, e = p.src_start, p.src_end
        moved += (abs(s - p.src_start) > 1e-6) + (abs(e - p.src_end) > 1e-6)
        out.append(Piece(round(max(0.0, s), 3), round(e, 3)))
    return out, moved
