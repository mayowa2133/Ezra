"""`ezra run <campaign>`: the whole supervised loop, plus the optimizer.

  every source: analyze → find candidates → rank
  render the top N publishable candidates → review queue
  (--autonomous) approve PASS clips and publish within posting limits

Autonomous publishing is opt-in twice over: the campaign must not require human
approval AND EZRA_ALLOW_AUTONOMOUS=1 must be set. Only clips whose compliance is
PASS (not REVIEW_REQUIRED) are ever auto-approved.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from . import analysis, analytics, audit, campaigns, candidates, db, metadata, publishing, render, review, sources
from .db.models import Campaign

Progress = Callable[[float, str], None]


def run_campaign(campaign: str | int, render_top: int = 5, max_candidates: int = 40, autonomous: bool = False,
                 spec: dict[str, Any] | None = None, progress: Progress | None = None) -> dict[str, Any]:
    say: Progress = progress or (lambda f, m: None)
    camp = campaigns.get(campaign)
    srcs = sources.list_sources(camp.slug)
    if not srcs:
        raise LookupError(f"campaign {camp.slug} has no sources: `ezra source add <file> --campaign {camp.slug}`")
    n = len(srcs)
    for i, src in enumerate(srcs):
        base = i / n * 0.7
        def analyzing(f: float, m: str, b: float = base, sid: int = src.id) -> None:
            say(b + f * 0.5 / n, f"source {sid}: {m}")

        def finding(f: float, m: str, b: float = base) -> None:
            say(b + (0.5 + f * 0.5) / n, m)

        if src.status != "analyzed":
            analysis.analyze_source(src.id, progress=analyzing)
        if not candidates.list_candidates(camp.slug, src.id):
            candidates.find_candidates(src.id, camp.slug, max_candidates, progress=finding)
    say(0.72, "rendering the strongest candidates")
    clips = render.render_top(camp.slug, top=render_top, spec=spec,
                              progress=lambda f, m: say(0.72 + 0.23 * f, m))
    report: dict[str, Any] = {"campaign": camp.slug, "sources": n,
                              "candidates": len(candidates.list_candidates(camp.slug, include_failed=True)),
                              "publishable": len(candidates.list_candidates(camp.slug)),
                              "rendered": [c.id for c in clips], "review_queue": len(review.queue(camp.slug))}
    if autonomous:
        report["autonomous"] = _autonomous(camp, clips)
    say(1.0, "done")
    return report


def autonomous_allowed(camp: Campaign) -> tuple[bool, str]:
    if camp.human_approval_required:
        return False, "campaign requires human approval (set human_approval_required: false to allow)"
    if os.environ.get("EZRA_ALLOW_AUTONOMOUS") != "1":
        return False, "EZRA_ALLOW_AUTONOMOUS=1 is not set"
    return True, "ok"


def _autonomous(camp: Campaign, clips: list[Any]) -> dict[str, Any]:
    ok, why = autonomous_allowed(camp)
    if not ok:
        return {"published": [], "skipped": why}
    published, held = [], []
    for c in clips:
        cand = candidates.get(c.candidate_id)
        if cand.compliance_status != "PASS":
            held.append({"clip_id": c.id, "reason": f"compliance {cand.compliance_status}"})
            continue
        review.approve(c.id, actor="autonomous", notes="auto-approved: compliance PASS")
        metadata.generate(c.id, camp.allowed_platforms)
        res = publishing.publish_clip(c.id, camp.allowed_platforms, confirm=True, actor="autonomous")
        if res["problems"]:
            held.append({"clip_id": c.id, "reason": "; ".join(res["problems"])})
        else:
            published.append(c.id)
    audit.record("campaign.autonomous_run", "campaign", camp.id, published=published, held=held)
    return {"published": published, "held": held}


def optimize(campaign: str | int, apply: bool = False) -> dict[str, Any]:
    """Suggest scoring-weight changes from how each factor relates to views in
    this campaign's results; `apply` writes them to the campaign."""
    import math

    import numpy as np

    camp = campaigns.get(campaign)
    obs = analytics.observations(camp.id)
    current = campaigns.normalized_weights(camp.weights)
    ins = analytics.insights(camp.id)
    out: dict[str, Any] = {"campaign": camp.slug, "n_posts": len(obs), "current_weights": current,
                           "observations": ins["observations"], "calibration": ins["calibration"]}
    if len(obs) < 8:
        out.update(suggested_weights=None, note=f"need at least 8 published posts with metrics (have {len(obs)})")
        return out
    logs = np.array([math.log(o["views"] + 1) for o in obs])
    corr: dict[str, float] = {}
    for f in current:
        xs = np.array([float(o.get(f) or 0) for o in obs])
        corr[f] = float(np.corrcoef(xs, logs)[0, 1]) if xs.std() > 0 else 0.0
    shrink = len(obs) / (len(obs) + 20)
    raw = {f: max(0.02, w * (1 + shrink * corr[f])) for f, w in current.items()}
    total = sum(raw.values())
    suggested = {f: round(v / total, 3) for f, v in raw.items()}
    out.update(factor_correlation={k: round(v, 3) for k, v in corr.items()}, suggested_weights=suggested,
               shrinkage=round(shrink, 2))
    if apply:
        with db.session() as s:
            row = s.get(Campaign, camp.id)
            if row is not None:
                row.weights = suggested
        audit.record("campaign.weights_updated", "campaign", camp.id, weights=suggested)
        candidates.rank(campaign=camp.slug, use_model=False)
        out["applied"] = True
    return out
