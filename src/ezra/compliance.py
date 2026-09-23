"""Compliance engine, independent of performance scoring.

Rules are evaluated at the stage where their evidence exists:
  candidate  duration, transcript words (profanity, forbidden words, competitors),
             source rights, speakers, forbidden topics / free-form rules
  render     subtitles, logo (from the render spec)
  publish    platform, hashtags, mentions, CTA, campaign window, posting limits

Outcome per rule: pass | fail | review | info. Overall:
  FAIL             any fail-severity rule is violated (never publishable)
  REVIEW_REQUIRED  nothing failed, but a review rule needs a human (or an LLM
                   critic flagged it); human approval can clear it
  PASS             everything checkable passed
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .db.models import Campaign, CampaignRule, Source

PROFANITY = {
    "fuck", "fucking", "fucked", "motherfucker", "shit", "shitty", "bullshit", "bitch", "asshole", "cunt",
    "dick", "pussy", "bastard", "damn", "goddamn", "piss", "slut", "whore",
}
STAGES = {
    "candidate": {"duration", "profanity", "forbidden_words", "competitors", "source_rights", "speakers",
                  "forbidden_topic", "freeform"},
    "render": {"subtitles", "logo", "duration"},   # duration again: edits change the length
    "publish": {"platforms", "hashtags", "mentions", "cta", "window", "posting_limits", "competitors",
                "forbidden_words", "profanity"},
}


@dataclass
class RuleResult:
    rule_id: int | None
    kind: str
    outcome: str        # pass | fail | review | info
    message: str
    stage: str


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def mentions(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", text.lower()) is not None


def _check(rule: CampaignRule, stage: str, ev: dict[str, Any]) -> RuleResult | None:
    k, p = rule.kind, rule.params or {}

    def res(outcome: str, message: str) -> RuleResult:
        if outcome == "violation":
            outcome = {"fail": "fail", "review": "review", "info": "info"}[rule.severity]
        return RuleResult(rule.id, k, outcome, message, stage)

    text = ev.get("transcript", "") if stage == "candidate" else ev.get("copy_text", "")
    if k == "duration" and ev.get("duration") is not None:
        d = ev["duration"]
        if d < p.get("min", 0) - 0.5:
            return res("violation", f"too short: {d:.1f}s < {p['min']:g}s")
        if d > p.get("max", 1e9) + 0.5:
            return res("violation", f"too long: {d:.1f}s > {p['max']:g}s")
        return res("pass", f"{d:.1f}s within {p.get('min', 0):g}-{p.get('max', 0):g}s")
    if k == "profanity":
        found = sorted({w for w in re.findall(r"[a-z']+", text.lower()) if w in PROFANITY})
        return res("violation", f"profanity: {', '.join(found)}") if found else res("pass", "no profanity")
    if k == "forbidden_words":
        found = [w for w in p.get("words", []) if mentions(text, w)]
        return res("violation", f"forbidden words: {', '.join(found)}") if found else res("pass", "none found")
    if k == "competitors":
        names = p.get("names", [])
        if not names:
            return res("review", "campaign forbids competitor mentions but lists no competitors; check manually")
        found = [n for n in names if mentions(text, n)]
        return res("violation", f"mentions competitor: {', '.join(found)}") if found else res("pass", "none mentioned")
    if k == "source_rights":
        src: Source | None = ev.get("source")
        if src is None:
            return None
        if src.rights_status == "rejected":
            return RuleResult(rule.id, k, "fail", f"source rights rejected ({src.rights_notes or 'no notes'})", stage)
        if src.rights_status != "authorized":
            return RuleResult(rule.id, k, "review", f"source rights {src.rights_status}: confirm authorization", stage)
        return res("pass", f"authorized ({src.rights_basis})")
    if k == "speakers":
        present = ev.get("speakers") or []
        allowed = p.get("allowed", [])
        mapping = ev.get("speaker_names") or {}
        named = [mapping.get(s, s) for s in present]
        unknown = [n for n in named if n not in allowed]
        if not present:
            return res("review", "no speaker labels; confirm only allowed speakers appear")
        return res("violation", f"speakers not confirmed as allowed: {', '.join(unknown)}") if unknown else \
            res("pass", "allowed speakers only")
    if k in ("forbidden_topic", "freeform"):
        verdict = (ev.get("llm_rule_verdicts") or {}).get(str(rule.id))
        if verdict is not None:
            ok = verdict.get("compliant")
            return res("pass" if ok else "violation",
                       f"model check: {verdict.get('reason', '')} (verify)" if not ok else f"model check passed: "
                       f"{verdict.get('reason', '')}")
        topic = p.get("topic") or p.get("text", "")
        keys = [t for t in re.findall(r"[a-z]{4,}", topic.lower()) if t not in ("mentions", "about", "content")]
        hit = [t for t in keys if mentions(text, t)]
        if hit:
            return res("violation", f"may touch '{topic}' (mentions {', '.join(hit)})")
        return RuleResult(rule.id, k, "review" if rule.severity != "info" else "info",
                          f"'{topic}': not machine-verifiable, needs a human check", stage)
    if k == "subtitles" and "render_spec" in ev:
        on = bool((ev["render_spec"] or {}).get("captions", True))
        return res("pass", "burned-in subtitles") if on else res("violation", "subtitles are required")
    if k == "logo" and "render_spec" in ev:
        has = bool((ev["render_spec"] or {}).get("logo_key"))
        return res("pass", "logo overlay present") if has else res("violation", "brand logo is required")
    if k == "platforms" and ev.get("platforms"):
        bad = [x for x in ev["platforms"] if x not in p.get("allowed", [])]
        return res("violation", f"platform not allowed: {', '.join(bad)}") if bad else res("pass", "allowed")
    if k == "hashtags" and "copy_text" in ev:
        missing = [t for t in p.get("tags", []) if t.lower() not in ev["copy_text"].lower()]
        return res("violation", f"missing hashtags {' '.join(missing)}") if missing else res("pass", "hashtags present")
    if k == "mentions" and "copy_text" in ev:
        missing = [m for m in p.get("handles", []) if m.lower() not in ev["copy_text"].lower()]
        return res("violation", f"missing mentions {' '.join(missing)}") if missing else res("pass", "mentions present")
    if k == "cta" and "copy_text" in ev:
        want = re.sub(r"\W+", " ", p.get("text", "").lower()).strip()
        have = re.sub(r"\W+", " ", ev["copy_text"].lower())
        if want and want in have:
            return res("pass", "CTA present")
        return res("violation", f"CTA missing: {p.get('text')}")
    if k == "window" and ev.get("when"):
        when = _utc(ev["when"])
        starts = _utc(datetime.fromisoformat(p["starts"])) if p.get("starts") else None
        ends = _utc(datetime.fromisoformat(p["ends"])) if p.get("ends") else None
        if starts and when < starts:
            return res("violation", f"before campaign start {p['starts']}")
        if ends and when > ends:
            return res("violation", f"after campaign end {p['ends']}")
        return res("pass", "inside campaign window")
    if k == "posting_limits" and ev.get("posting_counts") is not None:
        counts = ev["posting_counts"]  # {"day_total": n, "day_by_platform": {...}}
        per_day = p.get("per_day")
        if per_day is not None and counts.get("day_total", 0) >= per_day:
            return res("violation", f"daily posting limit {per_day} reached")
        for plat, lim in (p.get("per_platform_per_day") or {}).items():
            if counts.get("day_by_platform", {}).get(plat, 0) >= lim and plat in (ev.get("platforms") or []):
                return res("violation", f"{plat} daily limit {lim} reached")
        return res("pass", "within posting limits")
    if k == "geography":
        return RuleResult(rule.id, k, "info", rule.description, stage)
    return None


def evaluate(campaign: Campaign | None, stage: str, **evidence: Any) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {sorted(STAGES)}")
    results: list[RuleResult] = []
    for rule in (campaign.rules if campaign else []):
        if rule.kind not in STAGES[stage] and rule.kind != "geography":
            continue
        r = _check(rule, stage, evidence)
        if r is not None:
            results.append(r)
    outcomes = {r.outcome for r in results}
    status = "FAIL" if "fail" in outcomes else "REVIEW_REQUIRED" if "review" in outcomes else "PASS"
    return {"status": status, "stage": stage,
            "reasons": [asdict(r) for r in results if r.outcome != "pass"],
            "checked": [asdict(r) for r in results]}


def merge(*results: dict[str, Any]) -> dict[str, Any]:
    order = {"PASS": 0, "REVIEW_REQUIRED": 1, "FAIL": 2}
    status = max((r["status"] for r in results), key=order.__getitem__, default="PASS")
    return {"status": status, "reasons": [x for r in results for x in r["reasons"]],
            "checked": [x for r in results for x in r["checked"]]}
