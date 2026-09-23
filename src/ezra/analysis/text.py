"""Transcript-derived signals. All deterministic and cheap; each returns data
plus a confidence so downstream code never mistakes a heuristic for ground truth.

- topic segments: TextTiling (Hearst, 1997) over sentences with lexical cohesion
  (bag-of-words cosine between adjacent blocks), keywords by TF-IDF.
- speaking rate: words per minute overall and per segment.
- emotional-language cues: lexicon hits (loss, conflict, surprise, confession,
  superlatives, numbers/money), exclamations.
- question→answer transitions: a question followed by a different speaker.
- laughter/reactions: transcript markers ("haha", "(laughs)") only.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from ..transcription.base import Segment

STOP = set(["a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can", "could", "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from", "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me", "more", "most", "my", "myself", "no", "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other", "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should", "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "themselves", "then", "there", "these", "they", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would", "you", "your", "yours", "yourself", "yourselves", "um", "uh", "like", "yeah", "okay", "oh", "so", "really", "know", "think", "going", "get", "got", "gonna", "one", "also", "well", "right", "thing", "things", "actually", "kind", "sort", "lot"])

LEXICON: dict[str, set[str]] = {
    "loss": {"lost", "lose", "losing", "broke", "bankrupt", "fired", "failed", "failure", "debt", "quit",
             "crashed", "mistake", "mistakes", "fraud", "scam", "died", "death", "divorce", "worst"},
    "conflict": {"fight", "fought", "argue", "argued", "hate", "hated", "angry", "wrong", "lie", "lied",
                 "betrayed", "sued", "enemy", "war", "against", "never", "refuse", "refused"},
    "surprise": {"shocked", "shocking", "insane", "crazy", "unbelievable", "suddenly", "secret", "nobody",
                 "actually", "realized", "turns", "twist", "surprised", "wild"},
    "confession": {"admit", "confess", "honestly", "truth", "never", "told", "publicly", "embarrassed",
                   "ashamed", "afraid", "scared", "cried"},
    "stakes": {"million", "millions", "thousand", "billion", "everything", "life", "career", "company",
               "family", "money", "payroll", "revenue", "percent"},
    "superlative": {"best", "worst", "biggest", "hardest", "most", "only", "first", "last", "ever"},
    # competition / challenge formats (challenge channels, game shows, survival videos)
    "challenge": {"win", "wins", "won", "winner", "winning", "prize", "escape", "escaped", "caught", "catch",
                  "arrested", "arrest", "eliminated", "elimination", "survive", "survived", "challenge",
                  "compete", "competing", "hunt", "hunting", "chase", "trapped", "trap", "hide", "hiding",
                  "found", "busted", "betray", "betrayed", "steal", "stole", "cash", "cops", "police"},
    "danger": {"explode", "explosion", "exploded", "bomb", "detonator", "fire", "burning", "dangerous", "crash",
               "crashed", "falling", "fell", "drown", "injured", "hurt", "emergency", "run", "running"},
}
LAUGH = re.compile(r"\b(ha(ha)+|lol|lmao)\b|\((laughs?|laughter|laughing)\)|\[(laughs?|laughter)\]", re.I)
NUMBER = re.compile(r"(\$\s?\d[\d,.]*|\b\d[\d,.]*\s?(%|percent|k|m|million|thousand|billion)\b|\b\d{2,}\b)", re.I)


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9$']+", text.lower()) if t not in STOP and len(t) > 2]


def emotion_signals(text: str) -> dict[str, Any]:
    words = re.findall(r"[a-z']+", text.lower())
    hits = {k: sorted({w for w in words if w in lex}) for k, lex in LEXICON.items()}
    counts = {k: len(v) for k, v in hits.items()}
    numbers = NUMBER.findall(text)
    intensity = min(1.0, (sum(counts.values()) + text.count("!") + 0.5 * len(numbers)) / max(4.0, len(words) / 12))
    return {"counts": counts, "hits": {k: v for k, v in hits.items() if v}, "numbers": len(numbers),
            "exclamations": text.count("!"), "questions": text.count("?"),
            "laughter": bool(LAUGH.search(text)), "intensity": round(intensity, 3)}


def speaking_rate(segments: list[Segment]) -> dict[str, Any]:
    total_words = sum(len(s.words) for s in segments)
    speech = sum(max(0.0, s.end - s.start) for s in segments)
    per = [round(len(s.words) / max(0.1, s.end - s.start) * 60, 1) for s in segments]
    return {"wpm": round(total_words / speech * 60, 1) if speech else 0.0, "per_segment": per,
            "speech_seconds": round(speech, 1)}


def qa_transitions(segments: list[Segment]) -> list[dict[str, Any]]:
    out = []
    for a, b in zip(segments, segments[1:]):
        if a.text.rstrip().endswith("?"):
            out.append({"question_at": a.start, "answer_at": b.start, "question": a.text,
                        "speaker_change": bool(a.speaker and b.speaker and a.speaker != b.speaker)})
    return out


def laughter(segments: list[Segment]) -> list[dict[str, Any]]:
    return [{"at": s.start, "text": s.text} for s in segments if LAUGH.search(s.text)]


def _cosine(a: Counter, b: Counter) -> float:
    num = sum(a[t] * b[t] for t in set(a) & set(b))
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def topic_segments(segments: list[Segment], block: int = 6, min_sentences: int = 5) -> list[dict[str, Any]]:
    """TextTiling: cohesion between the `block` sentences before and after each
    gap; boundaries at depth-score peaks above mean - std/2."""
    n = len(segments)
    if n < 2 * min_sentences:
        return [_topic(segments, 0, n, segments)] if segments else []
    toks = [Counter(tokens(s.text)) for s in segments]
    gaps = []
    for g in range(1, n):
        left = sum((toks[i] for i in range(max(0, g - block), g)), Counter())
        right = sum((toks[i] for i in range(g, min(n, g + block))), Counter())
        gaps.append(_cosine(left, right))
    depth = []
    for i, v in enumerate(gaps):
        lpeak = max(gaps[max(0, i - block): i + 1])
        rpeak = max(gaps[i: i + block + 1])
        depth.append((lpeak - v) + (rpeak - v))
    mean = sum(depth) / len(depth)
    std = math.sqrt(sum((d - mean) ** 2 for d in depth) / len(depth))
    cutoff = mean - std / 2 if std else float("inf")
    bounds = [0]
    for i, d in sorted(enumerate(depth), key=lambda x: -x[1]):
        g = i + 1
        if d > cutoff and d > 0 and all(abs(g - b) >= min_sentences for b in bounds) and n - g >= min_sentences:
            bounds.append(g)
    bounds = sorted(bounds) + [n]
    return [_topic(segments, a, b, segments) for a, b in zip(bounds, bounds[1:])]


def _topic(all_segments: list[Segment], a: int, b: int, corpus: list[Segment]) -> dict[str, Any]:
    tf = Counter(t for s in all_segments[a:b] for t in tokens(s.text))
    df = Counter(t for s in corpus for t in set(tokens(s.text)))
    n = max(1, len(corpus))
    scored = sorted(tf, key=lambda t: -(tf[t] * math.log(1 + n / (1 + df[t]))))
    return {"start": all_segments[a].start, "end": all_segments[b - 1].end, "first_segment": a,
            "last_segment": b - 1, "keywords": scored[:6], "label": " / ".join(scored[:3])}
