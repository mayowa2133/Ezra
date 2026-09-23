"""Human review queue. Approval is the gate between rendering and publishing:
FAIL clips can never be approved; REVIEW_REQUIRED clips can be, and the
approval records which review reasons the reviewer accepted."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from . import audit, candidates, db, render
from .db.models import Clip, utcnow
from .storage import get_storage


def queue(campaign: str | int | None = None, include_decided: bool = False) -> list[dict[str, Any]]:
    statuses = ("rendered",) if not include_decided else ("rendered", "approved", "rejected", "exported", "published")
    clips = [c for c in render.list_clips(campaign) if c.status in statuses]
    items = [card(c) for c in clips]
    return sorted(items, key=lambda d: -(d["candidate"]["rank_score"] or 0))


def card(c: Clip) -> dict[str, Any]:
    """Everything a reviewer needs on one card."""
    v = render.current_version(c)
    cand = candidates.to_dict(c.candidate, detail=True)
    st = get_storage()
    return {
        "clip_id": c.id, "status": c.status, "title": c.title, "description": c.description, "hashtags": c.hashtags,
        "platform_metadata": c.platform_metadata, "review_notes": c.review_notes,
        "video_key": v.video_key if v else None, "thumbnail_key": v.thumbnail_key if v else None,
        "video_url": (st.url(v.video_key) if v and v.video_key else None),
        "duration": v.duration if v else None, "layout": v.layout_used if v else None,
        "edit_summary": v.edit_summary if v else None, "version": v.version if v else None,
        "candidate": cand, "compliance": cand["compliance"], "expected_value": cand["expected_value"],
    }


def approve(clip_id: int, actor: str = "reviewer", notes: str | None = None,
            acknowledge_review: bool = True) -> Clip:
    c = render.get_clip(clip_id)
    cand = c.candidate
    if c.status not in ("rendered", "rejected", "approved"):
        raise ValueError(f"clip {clip_id} is {c.status}; only rendered clips can be approved")
    if render.current_version(c) is None:
        raise ValueError(f"clip {clip_id} has no rendered version")
    if cand.compliance_status == "FAIL":
        raise PermissionError(f"clip {clip_id} fails campaign compliance and cannot be approved: "
                              + "; ".join(r["message"] for r in cand.compliance_reasons))
    review_reasons = [r["message"] for r in (cand.compliance_reasons or []) if r.get("outcome") == "review"]
    if review_reasons and not acknowledge_review:
        raise PermissionError("clip needs review: " + "; ".join(review_reasons))
    with db.session() as s:
        row = s.get(Clip, clip_id)
        assert row is not None
        row.status = "approved"
        row.reviewed_at = utcnow()
        row.reviewed_by = actor
        row.review_notes = notes
    audit.record("clip.approved", "clip", clip_id, actor=actor, notes=notes, acknowledged_review=review_reasons)
    return render.get_clip(clip_id)


def reject(clip_id: int, actor: str = "reviewer", reason: str | None = None) -> Clip:
    c = render.get_clip(clip_id)
    if c.status == "published":
        raise ValueError(f"clip {clip_id} is already published")
    with db.session() as s:
        row = s.get(Clip, clip_id)
        assert row is not None
        row.status = "rejected"
        row.reviewed_at = utcnow()
        row.reviewed_by = actor
        row.review_notes = reason
    audit.record("clip.rejected", "clip", clip_id, actor=actor, reason=reason)
    return render.get_clip(clip_id)


def update(clip_id: int, title: str | None = None, description: str | None = None,
           hashtags: list[str] | None = None, platform_metadata: dict[str, Any] | None = None,
           actor: str = "reviewer") -> Clip:
    with db.session() as s:
        row = s.get(Clip, clip_id)
        if row is None:
            raise LookupError(f"no clip {clip_id}")
        if title is not None:
            row.title = title.strip()[:300]
        if description is not None:
            row.description = description
        if hashtags is not None:
            row.hashtags = [h if h.startswith("#") else f"#{h}" for h in (x.strip() for x in hashtags) if h]
        if platform_metadata is not None:
            merged = dict(row.platform_metadata or {})
            for plat, meta in platform_metadata.items():
                merged[plat] = {**merged.get(plat, {}), **meta}
            row.platform_metadata = merged
    audit.record("clip.updated", "clip", clip_id, actor=actor)
    return render.get_clip(clip_id)


def pending_count(campaign: str | int | None = None) -> int:
    with db.session() as s:
        q = select(Clip).where(Clip.status == "rendered")
        return len(list(s.scalars(q)))
