"""YouTube Analytics API v2 for Ezra's posts: watch time, average view duration and percentage,
shares and subscribers, per video, stored as snapshots (never overwriting earlier ones).

Only metrics the API documents for per-video reports are requested:
  views, likes, comments, shares, estimatedMinutesWatched, averageViewDuration,
  averageViewPercentage, subscribersGained, subscribersLost
Impressions and click-through rate are not available from this API (they live in the bulk
Reporting API), so they are not requested and never invented.

Analytics data lags one to three days and is keyed by date in US Pacific time. The lifetime live
view count still comes from the Data API (`statistics`), which earnings use; these snapshots
(provider "youtube-analytics") feed learning and are left out of view counting.

Requires the yt-analytics.readonly scope, which Ezra requests at connect time.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .. import db, metrics
from ..db.models import Post, PublishAccount
from .base import PublishError, RetryablePublishError
from .youtube import ANALYTICS, YouTubePublisher

log = logging.getLogger("ezra.youtube.analytics")
REPORTS = "https://youtubeanalytics.googleapis.com/v2/reports"
PROVIDER = "youtube-analytics"
METRICS = ("views", "likes", "comments", "shares", "estimatedMinutesWatched", "averageViewDuration",
           "averageViewPercentage", "subscribersGained", "subscribersLost")
BATCH = 200                                    # the API's maxResults for per-video reports
PACIFIC = ZoneInfo("America/Los_Angeles")


def report(pub: YouTubePublisher, account: dict[str, Any], video_ids: list[str], start: date,
           end: date) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Per-video totals over [start, end]: ({video_id: {metric: value}}, raw response)."""
    out: dict[str, dict[str, Any]] = {}
    raw: dict[str, Any] = {"responses": []}
    for i in range(0, len(video_ids), BATCH):
        chunk = video_ids[i:i + BATCH]
        r = pub._call(account, "GET", REPORTS, "analytics report", params={
            "ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
            "metrics": ",".join(METRICS), "dimensions": "video", "filters": "video==" + ",".join(chunk),
            "sort": "-views", "maxResults": str(BATCH)})
        body = r.json()
        raw["responses"].append(body)
        out.update(parse(body))
    return out, raw


def parse(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A resultTable → {video: {metric: value}} using the column headers (column order isn't assumed)."""
    names = [h["name"] for h in body.get("columnHeaders") or []]
    rows = body.get("rows") or []
    out = {}
    for row in rows:
        rec = dict(zip(names, row))
        vid = rec.pop("video", None)
        if vid:
            out[vid] = rec
    return out


def normalize(rec: dict[str, Any]) -> dict[str, Any]:
    """API names → Ezra's snapshot fields. Missing stays missing."""
    def num(k: str) -> float | None:
        v = rec.get(k)
        return None if v is None else float(v)

    out = {"views": num("views"), "likes": num("likes"), "comments": num("comments"), "shares": num("shares"),
           "watch_minutes": num("estimatedMinutesWatched"), "avg_watch_seconds": num("averageViewDuration"),
           "avg_view_pct": num("averageViewPercentage"), "followers_gained": num("subscribersGained"),
           "subscribers_lost": num("subscribersLost")}
    ints = ("views", "likes", "comments", "shares", "followers_gained", "subscribers_lost")
    return {k: (int(v) if k in ints else round(v, 3)) for k, v in out.items() if v is not None}


def sync(campaign_id: int | None = None, pub: YouTubePublisher | None = None) -> dict[str, Any]:
    """One analytics snapshot per uploaded YouTube post, window = upload date → today (Pacific)."""
    from . import _account_ctx, _youtube

    pub = pub or _youtube()
    with db.session() as s:
        q = select(Post).where(Post.provider == "youtube", Post.external_id.is_not(None),
                               Post.status.in_(("published", "scheduled")))
        if campaign_id is not None:
            q = q.where(Post.campaign_id == campaign_id)
        posts = list(s.scalars(q))
        accounts = {a.id: a for a in s.scalars(select(PublishAccount).where(PublishAccount.provider == "youtube"))}
    by_account: dict[int, list[Post]] = {}
    for p in posts:
        if p.account_id in accounts:
            by_account.setdefault(p.account_id, []).append(p)
    today = datetime.now(PACIFIC).date()
    stored, errors, skipped = 0, [], []
    for acc_id, group in by_account.items():
        acc = accounts[acc_id]
        scopes = (acc.meta or {}).get("scopes") or []
        if scopes and ANALYTICS not in scopes:
            skipped.append({"account_id": acc_id, "reason": "analytics permission not granted; reconnect"})
            continue
        start = min(_upload_day(p) for p in group)
        try:
            rows, _raw = report(pub, _account_ctx(acc), [p.external_id for p in group if p.external_id], start,
                                today)
        except (PublishError, RetryablePublishError) as e:
            errors.append({"account_id": acc_id, "error_code": getattr(e, "code", None), "error": str(e)[:300]})
            log.warning("youtube analytics failed", extra={"account_id": acc_id, "code": getattr(e, "code", None)})
            continue
        window_start = datetime.combine(start, datetime.min.time(), PACIFIC).astimezone(UTC)
        window_end = datetime.combine(today + timedelta(days=1), datetime.min.time(), PACIFIC).astimezone(UTC)
        for p in group:
            rec = rows.get(p.external_id or "")
            if rec is None:                       # no data yet (analytics lag) — record nothing, not zeros
                skipped.append({"post_id": p.id, "reason": "no analytics rows yet (1-3 day lag)"})
                continue
            metrics.record(p.id, provider=PROVIDER, window_start=window_start, window_end=window_end,
                           raw={"row": rec, "start": start.isoformat(), "end": today.isoformat(),
                                "api": "youtubeAnalytics.reports v2"}, **normalize(rec))
            stored += 1
        log.info("youtube analytics synced", extra={"account_id": acc_id, "posts": len(group)})
    return {"snapshots": stored, "skipped": skipped, "errors": errors}


def _upload_day(p: Post) -> date:
    when = db.aware(p.published_at) or db.aware(p.created_at) or datetime.now(UTC)
    return when.astimezone(PACIFIC).date()
