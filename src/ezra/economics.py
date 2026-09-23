"""Campaign economics: earnings from observed views, expected value for
candidates, and margin after processing cost.

Earnings (per public post, per campaign):
  counted views  = latest snapshot inside the tracking window (if one is set)
  qualified      = counted views if views >= min_qualified_views and the
                   platform is allowed, else 0
  estimated      = min(qualified / 1000 * CPM, max_payout_per_clip)
  campaign total = min(sum(estimated), budget)
Confirmed revenue comes from RevenueRecords (entered or imported); estimates
are never presented as confirmed.

Expected value for a candidate is Monte-Carlo over a log-normal view model:
  log(views) ~ Normal(mu + beta * (rank - 50) / 50, sigma)
mu/sigma come from the campaign's own public posts (shrunk toward a prior with
pseudo-count 5); with no history the prior is used and the result is labelled
"prior". The p10/p90 interval is always reported.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

import numpy as np
from sqlalchemy import select

from . import campaigns, costs, db
from .db.models import Campaign, MetricSnapshot, Post, RevenueRecord

PRIOR_MU = math.log(2500.0)     # a middling clip on a small account
PRIOR_SIGMA = 1.6
PRIOR_BETA = 0.9                # log-views per unit of (rank-50)/50
SHRINK = 5.0
DEFAULT_RENDER_MINUTES = 1.5


def payout(views: int, c: Campaign, platform: str | None = None) -> dict[str, Any]:
    eligible = platform is None or not c.allowed_platforms or platform in c.allowed_platforms
    qualified = views if (eligible and views >= (c.min_qualified_views or 0)) else 0
    est = qualified / 1000.0 * (c.cpm or 0.0)
    capped = c.max_payout_per_clip is not None and est > c.max_payout_per_clip
    if c.max_payout_per_clip is not None:
        est = min(est, c.max_payout_per_clip)
    return {"qualified_views": qualified, "estimated": round(est, 2), "capped": capped, "eligible": eligible}


def counted_views(post: Post, c: Campaign) -> tuple[int, Any]:
    """Views from the latest snapshot inside the tracking window."""
    with db.session() as s:
        q = select(MetricSnapshot).where(MetricSnapshot.post_id == post.id, MetricSnapshot.views.is_not(None))
        if c.tracking_window_days and post.published_at:
            q = q.where(MetricSnapshot.captured_at <= post.published_at + timedelta(days=c.tracking_window_days))
        snap = s.scalar(q.order_by(MetricSnapshot.captured_at.desc(), MetricSnapshot.id.desc()).limit(1))
    return (snap.views or 0, snap) if snap else (0, None)


def earnings(campaign_ref: str | int) -> dict[str, Any]:
    c = campaigns.get(campaign_ref)
    with db.session() as s:
        posts = list(s.scalars(select(Post).where(Post.campaign_id == c.id).order_by(Post.id)))
        confirmed = list(s.scalars(select(RevenueRecord).where(RevenueRecord.campaign_id == c.id)))
    rows, total = [], 0.0
    for p in posts:
        if p.visibility != "public" or p.status != "published":
            continue
        views, snap = counted_views(p, c)
        pay = payout(views, c, p.platform)
        total += pay["estimated"]
        rows.append({"post_id": p.id, "clip_id": p.clip_id, "platform": p.platform, "url": p.url,
                     "views": views, "metrics_at": snap.captured_at.isoformat() if snap else None,
                     "metrics_provider": snap.provider if snap else None, **pay,
                     "confirmed": round(sum(r.amount for r in confirmed if r.post_id == p.id), 2) or None})
    budget_capped = c.budget is not None and total > c.budget
    gross_est = min(total, c.budget) if c.budget is not None else total
    confirmed_total = round(sum(r.amount for r in confirmed), 2)
    cost = costs.summary(c.id)
    basis = confirmed_total if confirmed_total else gross_est
    return {
        "campaign": c.slug, "currency": c.currency, "cpm": c.cpm, "min_qualified_views": c.min_qualified_views,
        "max_payout_per_clip": c.max_payout_per_clip, "budget": c.budget,
        "tracking_window_days": c.tracking_window_days, "posts": rows,
        "views": sum(r["views"] for r in rows), "qualified_views": sum(r["qualified_views"] for r in rows),
        "qualifying_posts": sum(1 for r in rows if r["qualified_views"]),
        "gross_estimated": round(gross_est, 2), "budget_capped": budget_capped,
        "confirmed": confirmed_total, "costs": cost,
        "margin_estimate": round(basis - cost["total_usd"], 2),
        "note": "estimated = observed views x CPM under campaign rules; confirmed = recorded payouts",
    }


def record_revenue(campaign_ref: str | int, amount: float, post_id: int | None = None, source: str = "manual",
                   notes: str | None = None) -> RevenueRecord:
    c = campaigns.get(campaign_ref)
    with db.session() as s:
        r = RevenueRecord(campaign_id=c.id, post_id=post_id, amount=amount, currency=c.currency, source=source,
                          notes=notes)
        s.add(r)
        s.flush()
        return r


# --- expected value -------------------------------------------------------------

def view_model(campaign_id: int | None) -> dict[str, Any]:
    """Log-normal parameters from observed public posts, shrunk toward the prior."""
    from .analytics import observations

    obs = observations(campaign_id=campaign_id)
    logs = [math.log(o["views"] + 1) for o in obs if o["views"] is not None]
    n = len(logs)
    if n == 0:
        return {"mu": PRIOR_MU, "sigma": PRIOR_SIGMA, "beta": PRIOR_BETA, "n": 0, "basis": "prior"}
    mean = sum(logs) / n
    var = sum((x - mean) ** 2 for x in logs) / max(1, n - 1)
    mu = (n * mean + SHRINK * PRIOR_MU) / (n + SHRINK)
    sigma = math.sqrt((n * var + SHRINK * PRIOR_SIGMA ** 2) / (n + SHRINK)) if n > 1 else PRIOR_SIGMA
    beta = PRIOR_BETA
    scored = [(o["rank_score"], math.log(o["views"] + 1)) for o in obs if o.get("rank_score") is not None]
    if len(scored) >= 8:
        xs = np.array([(r - 50) / 50 for r, _ in scored])
        ys = np.array([y for _, y in scored])
        if xs.std() > 1e-6:
            slope = float(np.cov(xs, ys, bias=True)[0, 1] / xs.var())
            beta = (len(scored) * slope + SHRINK * PRIOR_BETA) / (len(scored) + SHRINK)
    return {"mu": mu, "sigma": sigma, "beta": beta, "n": n, "basis": f"{n} observed posts"}


def expected_value(c: Campaign, rank_score: float, model: dict[str, Any] | None = None,
                   render_minutes: float = DEFAULT_RENDER_MINUTES, n_platforms: int | None = None,
                   samples: int = 4000, seed: int = 7) -> dict[str, Any]:
    m = model or view_model(c.id)
    rng = np.random.default_rng(seed)
    mu = m["mu"] + m["beta"] * (rank_score - 50) / 50
    views = np.exp(rng.normal(mu, m["sigma"], samples))
    qualified = np.where(views >= (c.min_qualified_views or 0), views, 0.0)
    pay = qualified / 1000.0 * (c.cpm or 0.0)
    if c.max_payout_per_clip is not None:
        pay = np.minimum(pay, c.max_payout_per_clip)
    platforms = n_platforms or max(1, len(c.allowed_platforms or [1]))
    per_post = float(pay.mean())
    compute_cost = render_minutes / 60 * costs.rate("compute_per_hour")
    ev = per_post * platforms
    return {
        "expected_views_median": int(math.exp(mu)),
        "p_qualify": round(float((qualified > 0).mean()), 3),
        "ev_per_post": round(per_post, 2),
        "ev_per_render": round(ev - compute_cost, 2),
        "ev_per_compute_minute": round((ev - compute_cost) / max(0.1, render_minutes), 2),
        "p10": round(float(np.percentile(pay, 10)) * platforms, 2),
        "p90": round(float(np.percentile(pay, 90)) * platforms, 2),
        "platforms": platforms, "basis": m["basis"], "currency": c.currency,
        "note": "ranking-based estimate with wide uncertainty, not a prediction",
    }
