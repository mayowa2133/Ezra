"""Campaign economics.

  qualified_views = views if views >= minimum_views else 0      (per post)
  estimated       = min(qualified_views / 1000 * CPM, maximum_payout)
  campaign total  = min(sum(estimated), budget)                  (if a budget is set)

`actual_payout`, when recorded, is what the campaign platform really paid.
"""

from __future__ import annotations

from typing import Any

from . import campaigns, db
from .campaigns import CampaignSpec

LATEST_METRIC = (
    "SELECT m.* FROM metrics m WHERE m.post_id = ? "
    "ORDER BY m.captured_at DESC, m.id DESC LIMIT 1"
)


def estimate(views: int, spec: CampaignSpec) -> dict[str, float]:
    qualified = views if views >= spec.minimum_views else 0
    est = qualified / 1000 * spec.rate.cpm
    if spec.maximum_payout is not None:
        est = min(est, spec.maximum_payout)
    return {"qualified_views": qualified, "estimated": round(est, 2)}


def posts_with_latest(campaign_ref: str | int | None = None,
                      include_private: bool = False) -> list[dict[str, Any]]:
    """Posts with their latest metrics. Private test posts are left out unless asked
    for: they earn nothing and would drag every average toward zero."""
    sql = ("SELECT p.*, c.campaign_id, c.title AS clip_title, c.ai_score, c.hook_type, "
           "c.opening_words, c.start_time, c.end_time, c.topic FROM posts p "
           "JOIN clips c ON c.id = p.clip_id")
    where, args = [], []
    if campaign_ref is not None:
        where.append("c.campaign_id = ?")
        args.append(campaigns.get(campaign_ref)["id"])
    if not include_private:
        where.append("p.visibility = 'public'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    with db.connect() as conn:
        out = db.rows(conn.execute(sql + " ORDER BY p.id", tuple(args)))
        for p in out:
            m = conn.execute(LATEST_METRIC, (p["id"],)).fetchone()
            p["views"] = m["views"] if m else 0
            for k in ("likes", "comments", "shares", "saves"):
                p[k] = m[k] if m else None
            p["metrics_at"] = m["captured_at"] if m else None
    return out


def report(campaign_ref: str | int) -> dict[str, Any]:
    spec = campaigns.spec(campaign_ref)
    posts = posts_with_latest(campaign_ref)
    total_est = 0.0
    for p in posts:
        p.update(estimate(p["views"], spec))
        total_est += p["estimated"]
    capped = min(total_est, spec.budget) if spec.budget is not None else total_est
    actual = [p["actual_payout"] for p in posts if p["actual_payout"] is not None]
    return {
        "campaign": campaigns.get(campaign_ref)["slug"],
        "cpm": spec.rate.cpm, "minimum_views": spec.minimum_views,
        "maximum_payout": spec.maximum_payout, "budget": spec.budget,
        "posts": posts,
        "total_views": sum(p["views"] for p in posts),
        "qualified_views": sum(p["qualified_views"] for p in posts),
        "estimated_revenue": round(capped, 2),
        "budget_capped": spec.budget is not None and total_est > spec.budget,
        "actual_revenue": round(sum(actual), 2) if actual else None,
    }
