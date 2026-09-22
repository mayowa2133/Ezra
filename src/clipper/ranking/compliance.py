"""Deterministic campaign checks. These run on every candidate before the
judge sees it and again on copy before publishing; judgment calls
("creator visible", tone) are left to the agent and the human reviewer."""

from __future__ import annotations

import re
from typing import Any

from ..campaigns import CampaignSpec

# Deliberately short: the goal is catching the obvious rejections, and the
# agent is told to flag anything subtler.
PROFANITY = {
    "fuck", "fucking", "fucked", "motherfucker", "shit", "shitty", "bullshit", "bitch",
    "asshole", "cunt", "dick", "pussy", "bastard", "damn", "goddamn", "piss", "slut", "whore",
}


def _mentions(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", text.lower()) is not None


def check_clip(spec: CampaignSpec, transcript: str, duration: float) -> list[str]:
    issues: list[str] = []
    req = spec.requirements
    if duration < req.min_duration - 0.5:
        issues.append(f"too short: {duration:.1f}s < {req.min_duration:g}s minimum")
    if duration > req.max_duration + 0.5:
        issues.append(f"too long: {duration:.1f}s > {req.max_duration:g}s maximum")
    forbidden = " ".join(spec.forbidden).lower()
    if "profan" in forbidden or "swear" in forbidden or "curs" in forbidden:
        found = sorted({w for w in re.findall(r"[a-z']+", transcript.lower()) if w in PROFANITY})
        if found:
            issues.append(f"profanity: {', '.join(found)}")
    for term in spec.forbidden_terms:
        if _mentions(transcript, term):
            issues.append(f"forbidden term: {term}")
    for comp in spec.competitors:
        if _mentions(transcript, comp):
            issues.append(f"competitor mention: {comp}")
    return issues


def check_copy(spec: CampaignSpec, copy: dict[str, Any], platforms: list[str]) -> list[str]:
    issues: list[str] = []
    for platform in platforms:
        text = copy_text(copy, platform)
        if not text.strip():
            issues.append(f"{platform}: no copy written")
            continue
        if spec.requires_hashtag:
            missing = [h for h in spec.hashtags if h.lower() not in text.lower()]
            if missing:
                issues.append(f"{platform}: missing required hashtag(s) {', '.join(missing)}")
        for term in [*spec.forbidden_terms, *spec.competitors]:
            if _mentions(text, term):
                issues.append(f"{platform}: copy mentions forbidden '{term}'")
    return issues


def copy_text(copy: dict[str, Any], platform: str) -> str:
    entry = copy.get(platform) or {}
    if isinstance(entry, str):
        return entry
    return " ".join(str(entry.get(k) or "") for k in ("title", "caption", "description"))
