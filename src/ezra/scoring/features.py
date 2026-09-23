"""Cheap, deterministic features of a candidate window. Computed for every
window the scout proposes (hundreds per hour of footage) before any model or
render is spent on it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..analysis.text import LEXICON, NUMBER, emotion_signals, tokens
from ..transcription.base import Segment, Word, ends_sentence, join_words

CONNECTOR_START = {"so", "and", "but", "or", "because", "anyway", "also", "then", "plus", "which", "like"}
DANGLING_START = {"he", "she", "they", "it", "that", "this", "those", "these", "him", "her", "them", "there"}
FILLERS = {"um", "uh", "erm", "er", "hmm", "mm", "uhm", "ah"}
BACKREF = re.compile(r"\b(like i said|as i said|as we (discussed|said|mentioned)|earlier|going back to|"
                     r"you mentioned|we talked about|remember when)\b", re.I)
CONTRARIAN = re.compile(r"\b(most people|nobody|no one|everyone thinks|disagree|actually|the truth is|"
                        r"myth|wrong|overrated|underrated|never|stop|unpopular|controversial)\b", re.I)
OPINION = re.compile(r"\b(i think|i believe|should|shouldn't|never|always|the best|the worst|you need to|"
                     r"you have to|don't)\b", re.I)
LESSON = re.compile(r"\b(that's why|the lesson|would have|if i|if you|my advice|here's the thing|"
                    r"the point is|what i learned|that one|the key|so if you|saved me|changed)\b", re.I)


@dataclass
class Window:
    source_id: int
    start: float
    end: float
    segments: list[Segment]
    before: list[Segment] = field(default_factory=list)
    after: list[Segment] = field(default_factory=list)
    topic: dict[str, Any] | None = None

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    @property
    def text(self) -> str:
        return join_words([w.w for w in self.words])

    @property
    def duration(self) -> float:
        return self.end - self.start


def _first_words(text: str, n: int) -> list[str]:
    return re.findall(r"[a-z0-9$']+", text.lower())[:n]


def extract(win: Window, silence: list[dict[str, float]], activity: list[float],
            face_timeline: list[Any] | None, scenes: list[dict[str, float]],
            topics: list[dict[str, Any]], corpus_df: dict[str, int], n_docs: int) -> dict[str, Any]:
    text = win.text
    first = win.segments[0].text if win.segments else ""
    last = win.segments[-1].text if win.segments else ""
    words = win.words
    n_words = len(words)
    first_tokens = _first_words(first, 3)
    speech = sum(w.e - w.s for w in words)
    dead_air = sum(max(0.0, min(r["end"], win.end) - max(r["start"], win.start)) for r in silence)
    gaps = [b.s - a.e for a, b in zip(words, words[1:])]
    long_gaps = sum(g for g in gaps if g > 0.7)
    em_all = emotion_signals(text)
    em_first = emotion_signals(first)
    em_last = emotion_signals(last)
    thirds = max(1, len(win.segments) // 3)
    em_head = emotion_signals(" ".join(s.text for s in win.segments[:thirds]))["intensity"]
    em_tail = emotion_signals(" ".join(s.text for s in win.segments[-thirds:]))["intensity"]
    toks = tokens(text)
    import math

    rarity = (sum(math.log(1 + n_docs / (1 + corpus_df.get(t, 0))) for t in set(toks)) / len(set(toks))) if toks else 0.0
    sec = [a for i, a in enumerate(activity) if win.start <= i < win.end]
    faces_in = [f for t, f in (face_timeline or []) if win.start <= t < win.end]
    cuts = sum(1 for sc in scenes if win.start < sc["start"] < win.end)
    topic_ids = {i for i, tp in enumerate(topics) if tp["start"] < win.end and tp["end"] > win.start}
    speakers = sorted({w.spk for w in words if w.spk})
    return {
        "duration": round(win.duration, 2), "n_words": n_words,
        "wpm": round(n_words / max(0.1, speech) * 60, 1) if speech else 0.0,
        "dead_air_ratio": round((dead_air + long_gaps) / max(0.1, win.duration), 3),
        "starts_with_connector": bool(first_tokens) and first_tokens[0] in CONNECTOR_START,
        "starts_with_dangling": bool(first_tokens) and first_tokens[0] in DANGLING_START,
        "starts_with_filler": bool(first_tokens) and first_tokens[0] in FILLERS,
        "first_sentence_words": len(first.split()),
        "first_person_claim": bool(re.match(r"^\s*(i|we|my|our)\b", first, re.I)),
        "first_is_question": first.rstrip().endswith("?"),
        "first_has_number": bool(NUMBER.search(first)),
        "first_lexicon": {k: v for k, v in em_first["counts"].items() if v},
        "first_intensity": em_first["intensity"],
        "intensity": em_all["intensity"], "lexicon": em_all["counts"], "numbers": em_all["numbers"],
        "questions": em_all["questions"], "exclamations": em_all["exclamations"], "laughter": em_all["laughter"],
        "ends_sentence": bool(words) and ends_sentence(words[-1].w),
        "last_intensity": em_last["intensity"], "last_has_lesson": bool(LESSON.search(last)),
        "ends_on_question": last.rstrip().endswith("?"),
        "last_has_number": bool(NUMBER.search(last)),
        "payoff_shift": round(em_tail - em_head, 3),
        "backrefs": len(BACKREF.findall(text)), "contrarian": len(CONTRARIAN.findall(text)),
        "opinion": len(OPINION.findall(text)), "you_address": len(re.findall(r"\byou\b", text, re.I)),
        "rarity": round(rarity, 3), "fillers": sum(1 for w in words if w.w.lower().strip(",.") in FILLERS),
        "visual_activity": round(sum(sec) / len(sec), 2) if sec else 0.0,
        "face_rate": round(sum(1 for f in faces_in if f) / len(faces_in), 3) if faces_in else None,
        "max_faces": max((len(f) for f in faces_in), default=0),
        "scene_cuts": cuts, "topics_spanned": len(topic_ids), "speakers": speakers,
        "n_speakers": len(speakers),
        "lexicon_hits": em_all["hits"],
        "vocab": sorted(set(toks))[:400],
    }
