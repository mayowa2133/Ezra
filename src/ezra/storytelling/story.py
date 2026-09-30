"""Helpers for writing a story timeline around its narration (used by each story's build script).

    tm = Timing.load(Path("narration/timing.json"))
    tm.at(3, "booker")      # timeline time to cut on: 0.08 s before that word in line 3
    tm.end_of(3)            # when line 3 stops
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

LEAD = 0.08            # cut this long before the word (the reference cuts ~0.1 s before a word onset)


class Timing:
    def __init__(self, lines: list[dict[str, Any]]):
        self.lines = lines

    @classmethod
    def load(cls, path: Path) -> Timing:
        return cls(json.loads(path.read_text()))

    def at(self, li: int, word: str, lead: float = LEAD) -> float:
        key = re.sub(r"[^a-z0-9]", "", word.lower())
        for w in self.lines[li]["words"]:
            if re.sub(r"[^a-z0-9]", "", w["w"].lower()).startswith(key):
                return round(w["s"] - lead, 3)
        raise KeyError(f"{word!r} not in line {li}: {self.lines[li]['text']}")

    def start_of(self, li: int) -> float:
        return float(self.lines[li]["start"])

    def end_of(self, li: int) -> float:
        return float(self.lines[li]["end"])

    def words(self) -> list[dict[str, Any]]:
        return [w for ln in self.lines for w in ln["words"]]


def steady_music(level: float, end: float, drops: tuple[tuple[float, float], ...] | list[tuple[float, float]] = (),
                 tail: float = 1.2
                 ) -> list[list[float]]:
    """Gain points (dB) for a steady bed, as the reference edits use (no ducking), with short
    silences at `drops` (start, end) and a fade after `end`."""
    pts: list[list[float]] = [[0.0, level]]
    for a, b in sorted(drops):
        pts += [[a - 0.05, level], [a + 0.3, -40.0], [b, -40.0], [b + 0.4, level]]
    pts += [[end, level], [end + tail, -40.0]]
    return pts


def fit_music(bed_end: float, story_end: float) -> dict[str, float]:
    """Offset/tempo so a music bed ending at `bed_end` ends on the story's last frame: a shorter
    story starts later in the track; a longer one is stretched (pitch kept)."""
    return {"offset": round(max(0.0, bed_end - story_end), 3), "tempo": round(min(1.0, bed_end / story_end), 4)}
