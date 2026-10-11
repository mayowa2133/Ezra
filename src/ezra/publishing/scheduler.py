"""Scheduler tick, run by the worker every ~15s: queue due posts, resolve
platform-side processing, and sync metrics on an interval."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select

from .. import db, jobs
from ..db.models import Job, Post, utcnow

METRICS_EVERY = timedelta(hours=float(os.environ.get("EZRA_METRICS_SYNC_HOURS", "6")))
BUFFER_STATUS_EVERY = timedelta(minutes=15)


def due_posts() -> list[int]:
    with db.session() as s:
        return list(s.scalars(select(Post.id).where(Post.status == "scheduled", Post.scheduled_at <= utcnow(),
                                                    Post.provider.not_in(("youtube", "buffer")))))


def tick() -> dict[str, Any]:
    queued = []
    for pid in due_posts():
        # YouTube and Buffer schedule remotely; remaining providers are submitted when due.
        jobs.enqueue("publish_post", {"post_id": pid}, dedupe_key=f"publish-{pid}", priority=40)
        with db.session() as s:
            p = s.get(Post, pid)
            if p is not None and p.status == "scheduled":
                p.status = "publishing"
        queued.append(pid)
    with db.session() as s:
        processing = s.scalar(select(Post.id).where(Post.external_id.is_not(None),
            Post.provider != "buffer", Post.status == "publishing").limit(1))
        buffer_processing = s.scalar(select(Post.id).where(Post.external_id.is_not(None), or_(
            and_(Post.provider == "buffer", Post.status == "publishing"),
            and_(Post.provider == "buffer", Post.status == "scheduled",
                 or_(Post.scheduled_at.is_(None), Post.scheduled_at <= utcnow())))).limit(1))
        last_status = s.scalar(select(Job).where(Job.kind == "refresh_post_status").order_by(Job.id.desc()).limit(1))
        last_sync = s.scalar(select(Job).where(Job.kind == "sync_metrics").order_by(Job.id.desc()).limit(1))
        published = s.scalar(select(Post.id).where(Post.status == "published").limit(1))
    buffer_due = buffer_processing is not None and (last_status is None
        or utcnow() - (db.aware(last_status.created_at) or utcnow()) >= BUFFER_STATUS_EVERY)
    if processing is not None or buffer_due:
        jobs.enqueue("refresh_post_status", {}, dedupe_key="refresh-post-status", priority=60)
    if published is not None and (last_sync is None or utcnow() - (db.aware(last_sync.created_at) or utcnow())
                                  > METRICS_EVERY):
        jobs.enqueue("sync_metrics", {}, dedupe_key="sync-metrics", priority=120)
    return {"queued_posts": queued}
