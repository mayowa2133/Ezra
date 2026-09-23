"""Deterministic factor scores from features. Transparent by design: every
factor carries a short explanation of what moved it. These are ranking
estimates, not predictions of virality, and they are stored with confidence
0.35 so the UI and the optimizer weigh them accordingly."""

from __future__ import annotations

import re
from typing import Any

HOOK_TYPES = ["loss", "confession", "contrarian", "question", "how_to", "story", "number", "shock",
              "prediction", "other"]
CONFIDENCE = 0.35


def clamp(v: float) -> float:
    return round(max(0.0, min(100.0, v)), 1)


def hook_type(first_sentence: str, f: dict[str, Any]) -> str:
    low = first_sentence.lower()
    lex = f.get("first_lexicon", {})
    if lex.get("loss"):
        return "loss"
    if re.search(r"\b(never (said|told)|admit|confess|honestly|the truth)\b", low):
        return "confession"
    if re.search(r"\b(most people|nobody|disagree|actually|myth|wrong|should never)\b", low):
        return "contrarian"
    if f.get("first_is_question"):
        return "question"
    if re.search(r"\b(how to|here's how|the way to|step)\b", low):
        return "how_to"
    if f.get("first_has_number"):
        return "number"
    if lex.get("surprise"):
        return "shock"
    if re.search(r"\b(will|going to be|in (five|ten|\d+) years|future)\b", low):
        return "prediction"
    if re.search(r"\b(when i|in (19|20)\d\d|one day|i was)\b", low):
        return "story"
    return "other"


def score(f: dict[str, Any], campaign: dict[str, Any] | None = None) -> tuple[dict[str, float], dict[str, str]]:
    why: dict[str, str] = {}

    # hook: does the opening create a reason to keep watching?
    h = 45.0
    notes = []
    stakes = sum(f["first_lexicon"].get(k, 0) for k in ("loss", "conflict", "confession", "surprise", "stakes"))
    if stakes:
        h += min(24, 9 * stakes)
        notes.append("stakes in the opening")
    if f["first_has_number"]:
        h += 9
        notes.append("concrete number")
    if f["first_person_claim"]:
        h += 6
        notes.append("first-person claim")
    if f["first_is_question"]:
        h += 5
        notes.append("opens on a question")
    if f["first_sentence_words"] <= 12:
        h += 5
        notes.append("short first sentence")
    elif f["first_sentence_words"] > 28:
        h -= 8
        notes.append("long winding opener")
    if f["starts_with_connector"] or f["starts_with_filler"]:
        h -= 18
        notes.append("opens mid-thought (connector/filler)")
    if f["starts_with_dangling"]:
        h -= 12
        notes.append("opens on an unresolved pronoun")
    why["hook"] = ", ".join(notes) or "neutral opening"
    hook = clamp(h)

    # retention: tension to the end, no dead air, sensible pace
    r = 55.0
    notes = []
    if f["ends_sentence"]:
        r += 8
    else:
        r -= 15
        notes.append("ends mid-sentence")
    r += max(-10.0, min(10.0, 40 * f["payoff_shift"]))
    if f["payoff_shift"] > 0.05:
        notes.append("intensity builds toward the end")
    if f["dead_air_ratio"] > 0.08:
        r -= min(25, 150 * (f["dead_air_ratio"] - 0.08))
        notes.append(f"{f['dead_air_ratio']:.0%} dead air")
    wpm = f["wpm"]
    if wpm and (wpm < 120 or wpm > 240):
        r -= 8
        notes.append(f"pace {wpm:.0f} wpm")
    if f["topics_spanned"] > 1:
        r -= 6 * (f["topics_spanned"] - 1)
        notes.append("drifts across topics")
    d = f["duration"]
    if d > 50:
        r -= (d - 50) * 0.6
        notes.append("long for short-form")
    why["retention"] = ", ".join(notes) or "steady"
    retention = clamp(r)

    # context: understandable without the episode
    c = 72.0
    notes = []
    if f["starts_with_dangling"]:
        c -= 22
        notes.append("unresolved pronoun at start")
    if f["starts_with_connector"]:
        c -= 12
        notes.append("starts mid-argument")
    if f["backrefs"]:
        c -= 12 * f["backrefs"]
        notes.append("refers back to earlier discussion")
    if not f["ends_sentence"]:
        c -= 8
    if f["first_person_claim"]:
        c += 6
    why["context"] = ", ".join(notes) or "stands alone"
    context = clamp(c)

    # emotion
    e = 30 + 70 * f["intensity"] + (6 if f["laughter"] else 0) + min(8, 3 * f["exclamations"])
    why["emotion"] = ", ".join(f"{k} words" for k, v in f["lexicon"].items() if v) or "flat"
    emotion = clamp(e)

    # novelty: contrarian framing and uncommon vocabulary for this source
    n = 35 + min(30, 10 * f["contrarian"]) + min(25, 12 * max(0.0, f["rarity"] - 1.0))
    why["novelty"] = f"{f['contrarian']} contrarian markers, vocabulary rarity {f['rarity']:.2f}"
    novelty = clamp(n)

    # discussion potential
    q = 35 + min(20, 8 * f["opinion"]) + min(15, 6 * f["questions"]) + min(12, 3 * f["you_address"]) \
        + min(15, 7 * f["contrarian"])
    why["discussion"] = f"{f['opinion']} opinion markers, {f['questions']} questions"
    discussion = clamp(q)

    # payoff: does the last line land?
    p = 45 + (18 if f["last_has_lesson"] else 0) + (10 if f["last_has_number"] else 0) \
        + 35 * f["last_intensity"] + (0 if f["ends_sentence"] else -20)
    if f.get("ends_on_question"):
        p -= 18
    why["payoff"] = ("ends on an open question (sets up something the clip never answers)" if f.get("ends_on_question")
                     else "ends on a lesson" if f["last_has_lesson"]
                     else "ends mid-sentence" if not f["ends_sentence"] else "plain ending")
    payoff = clamp(p)

    # visual: faces on screen, some movement, not too many cuts
    v = 50.0
    notes = []
    if f["face_rate"] is not None:
        v += 30 * (f["face_rate"] - 0.5)
        notes.append(f"face on screen {f['face_rate']:.0%}")
    if f["visual_activity"]:
        v += min(10, f["visual_activity"] / 3)
    if f["scene_cuts"] > 8:
        v -= 8
        notes.append("many cuts")
    why["visual"] = ", ".join(notes) or "no visual data"
    visual = clamp(v)

    # campaign fit
    fit = 65.0
    notes = []
    if campaign:
        brief = (campaign.get("brief") or "") + " " + (campaign.get("description") or "")
        brief_toks = set(re.findall(r"[a-z]{4,}", brief.lower()))
        hits = sorted(brief_toks & set(f.get("vocab", [])))
        if hits:
            fit += min(20, 6 * len(hits))
            notes.append(f"matches the brief ({', '.join(hits[:4])})")
        mn, mx = campaign.get("min_duration", 0), campaign.get("max_duration", 1e9)
        if not (mn <= f["duration"] <= mx):
            fit -= 30
            notes.append("outside duration limits")
    why["campaign_fit"] = ", ".join(notes) or "neutral"
    campaign_fit = clamp(fit)

    scores = {"hook": hook, "retention": retention, "context": context, "emotion": emotion, "novelty": novelty,
              "discussion": discussion, "payoff": payoff, "visual": visual, "campaign_fit": campaign_fit}
    return scores, why


def content_score(scores: dict[str, float], weights: dict[str, float]) -> float:
    total = sum(weights.values()) or 1.0
    return round(sum(scores[k] * w for k, w in weights.items() if k in scores) / total, 2)
