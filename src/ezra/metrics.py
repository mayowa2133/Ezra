"""Metrics: point-in-time snapshots per post, each with provenance.

Sources: the publishing adapters' platform APIs (views/likes/comments/...
where the API offers them), manual entry, and CSV import. Unavailable metrics
stay NULL; nothing is invented. Views at 1h / 24h / 7d are derived from the
snapshot history instead of being overwritten."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from . import db
from .db.models import MetricSnapshot, Post, PublishAccount, utcnow

FIELDS = ("views", "likes", "comments", "shares", "saves", "impressions", "avg_watch_seconds",
          "completion_rate", "followers_gained")


def record(post_id: int, provider: str = "manual", captured_at: datetime | None = None,
           raw: dict[str, Any] | None = None, **values: Any) -> MetricSnapshot:
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown metrics {sorted(unknown)}; allowed {FIELDS}")
    with db.session() as s:
        if s.get(Post, post_id) is None:
            raise LookupError(f"no post {post_id}")
        snap = MetricSnapshot(post_id=post_id, provider=provider, captured_at=captured_at or utcnow(), raw=raw or {},
                              **{k: v for k, v in values.items() if v is not None})
        s.add(snap)
        s.flush()
        return snap


def sync(campaign_id: int | None = None) -> dict[str, Any]:
    """Pull metrics for every published post from its provider's API."""
    from .publishing import _publisher, account_dict

    with db.session() as s:
        q = select(Post).where(Post.status == "published", Post.external_id.is_not(None))
        if campaign_id is not None:
            q = q.where(Post.campaign_id == campaign_id)
        posts = list(s.scalars(q))
        accounts = {a.id: a for a in s.scalars(select(PublishAccount))}
    stored, skipped, errors = 0, 0, []
    for p in posts:
        acc = accounts.get(p.account_id or -1)
        if acc is None or acc.provider == "local-export":
            skipped += 1
            continue
        pub = _publisher(acc.provider)
        try:
            m = pub.metrics(account_dict(acc) | {"credential_ref": acc.credential_ref, "meta": acc.meta or {}},
                            p.external_id)
        except Exception as e:  # one account's failure must not stop the sync
            errors.append({"post_id": p.id, "error": f"{type(e).__name__}: {e}"[:300]})
            continue
        if m is None:
            skipped += 1
            continue
        record(p.id, provider=f"{acc.provider}-api", raw=m.raw,
               **{k: getattr(m, k) for k in FIELDS if getattr(m, k) is not None})
        stored += 1
    return {"snapshots": stored, "skipped": skipped, "errors": errors}


def import_csv(text: str, provider: str = "csv") -> dict[str, Any]:
    """Columns: post_id or url, then any of FIELDS, optional captured_at (ISO)."""
    stored, missing = 0, []
    with db.session() as s:
        by_url = {p.url: p.id for p in s.scalars(select(Post).where(Post.url.is_not(None)))}
    for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        pid = row.get("post_id")
        post_id = int(pid) if pid and pid.strip().isdigit() else by_url.get((row.get("url") or "").strip())
        if post_id is None:
            missing.append(i)
            continue
        vals = {}
        for k in FIELDS:
            v = (row.get(k) or "").replace(",", "").strip()
            if v:
                vals[k] = float(v) if k in ("avg_watch_seconds", "completion_rate") else int(float(v))
        when = datetime.fromisoformat(row["captured_at"]) if row.get("captured_at") else None
        record(post_id, provider, when, None, **vals)
        stored += 1
    return {"snapshots": stored, "rows_without_post": missing}


def history(post_id: int) -> list[dict[str, Any]]:
    with db.session() as s:
        snaps = list(s.scalars(select(MetricSnapshot).where(MetricSnapshot.post_id == post_id)
                               .order_by(MetricSnapshot.captured_at)))
    return [snapshot_dict(x) for x in snaps]


def views_at(post: Post, hours: float) -> int | None:
    if post.published_at is None:
        return None
    cutoff = db.aware(post.published_at) + timedelta(hours=hours)  # type: ignore[operator]
    with db.session() as s:
        snap = s.scalar(select(MetricSnapshot).where(MetricSnapshot.post_id == post.id,
                                                     MetricSnapshot.captured_at <= cutoff,
                                                     MetricSnapshot.views.is_not(None))
                        .order_by(MetricSnapshot.captured_at.desc()).limit(1))
    return snap.views if snap else None


def snapshot_dict(x: MetricSnapshot) -> dict[str, Any]:
    return {"id": x.id, "post_id": x.post_id, "captured_at": db.aware(x.captured_at).isoformat(),  # type: ignore[union-attr]
            "provider": x.provider, **{k: getattr(x, k) for k in FIELDS}}
