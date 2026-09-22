"""Publishing gate + posting. Nothing leaves the machine unless a human
approved the clip, its copy passes the campaign checks, and the caller
passed confirm=True."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import campaigns, db
from ..analytics import metrics
from ..clipping import clips
from ..config import settings
from ..integrations.uploadpost import UploadPost, UploadPostError, metric_values, platform_results
from ..ranking import compliance


def preflight(clip_id: int, platforms: list[str]) -> list[str]:
    clip = clips.get(clip_id)
    spec = campaigns.spec(clip["campaign_id"])
    problems = []
    if clip["status"] != "approved":
        problems.append(f"clip {clip_id} is '{clip['status']}', not approved by a human")
    if not clip["video_path"] or not Path(clip["video_path"]).exists():
        problems.append(f"clip {clip_id} has no rendered video")
    copy = db.loads(clip["copy_json"], {})
    problems += compliance.check_copy(spec, copy, platforms)
    return problems


def _client() -> UploadPost:
    s = settings()
    return UploadPost(s.upload_post_api_key or "", s.upload_post_user or "")


def publish(clip_id: int, platforms: list[str], confirm: bool = False, dry_run: bool = False,
            scheduled_date: str | None = None, timezone: str | None = None,
            client: UploadPost | None = None) -> dict[str, Any]:
    problems = preflight(clip_id, platforms)
    if problems:
        return {"clip_id": clip_id, "published": False, "problems": problems}
    clip = clips.get(clip_id)
    copy = db.loads(clip["copy_json"], {})
    if dry_run or not confirm:
        return {"clip_id": clip_id, "published": False, "dry_run": True, "platforms": platforms,
                "video": clip["video_path"], "copy": copy,
                "note": "pass confirm=True to post" if not dry_run else "dry run"}
    up = client or _client()
    payload = up.upload(Path(clip["video_path"]), platforms, copy, scheduled_date, timezone)
    results = platform_results(payload)
    request_id = payload.get("request_id") or payload.get("job_id")
    posts = []
    with db.connect() as conn:
        for platform in platforms:
            r = results.get(platform, {})
            if r and r.get("success") is False:
                status = "failed"
            elif r.get("url") or r.get("post_id"):
                status = "published"
            else:
                status = "scheduled" if scheduled_date else "submitted"
            caption = compliance.copy_text(copy, platform)
            cur = conn.execute(
                "INSERT INTO posts (clip_id, platform, post_id, post_url, request_id, status, caption, "
                "response_json, posted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (clip_id, platform, str(r.get("post_id") or r.get("publish_id") or "") or None,
                 r.get("url"), request_id, status, caption, db.dumps(r or payload),
                 scheduled_date or db.now()))
            posts.append({"post_id": cur.lastrowid, "platform": platform, "status": status,
                          "url": r.get("url"), "error": r.get("error")})
    if any(p["status"] != "failed" for p in posts):
        clips.update(clip_id, status="published")
    return {"clip_id": clip_id, "published": True, "request_id": request_id, "posts": posts}


def list_posts(campaign_ref: str | int | None = None) -> list[dict[str, Any]]:
    from ..revenue import posts_with_latest

    return posts_with_latest(campaign_ref)


def refresh_status(client: UploadPost | None = None) -> list[dict[str, Any]]:
    """Resolve async uploads to real URLs / ids."""
    up = client or _client()
    with db.connect() as conn:
        pending = db.rows(conn.execute(
            "SELECT * FROM posts WHERE status = 'submitted' AND request_id IS NOT NULL"))
    updated = []
    for req_id in sorted({p["request_id"] for p in pending}):
        try:
            results = platform_results(up.status(req_id))
        except UploadPostError as e:
            updated.append({"request_id": req_id, "error": str(e)})
            continue
        with db.connect() as conn:
            for p in (x for x in pending if x["request_id"] == req_id):
                r = results.get(p["platform"])
                if not r:
                    continue
                status = "failed" if r.get("success") is False else (
                    "published" if (r.get("url") or r.get("post_id")) else "submitted")
                conn.execute("UPDATE posts SET status=?, post_url=?, post_id=?, response_json=? WHERE id=?",
                             (status, r.get("url") or p["post_url"],
                              str(r.get("post_id") or r.get("publish_id") or p["post_id"] or "") or None,
                              db.dumps(r), p["id"]))
                updated.append({"post_id": p["id"], "platform": p["platform"], "status": status})
    return updated


def sync_metrics(client: UploadPost | None = None) -> dict[str, Any]:
    """Pull per-post analytics from Upload-Post and store a snapshot for each
    of our posts it can match by platform post id or URL."""
    up = client or _client()
    refresh_status(up)
    with db.connect() as conn:
        ours = db.rows(conn.execute("SELECT * FROM posts WHERE status = 'published'"))
    stored, unmatched = 0, 0
    for platform in sorted({p["platform"] for p in ours}):
        rows = up.post_analytics(platform)
        index: dict[str, dict[str, Any]] = {}
        for row in rows:
            for key in ("post_id", "id", "platform_post_id", "url", "post_url", "permalink"):
                if row.get(key):
                    index[str(row[key])] = row
        for p in (x for x in ours if x["platform"] == platform):
            row = index.get(str(p["post_id"] or "")) or index.get(str(p["post_url"] or ""))
            if row is None:
                unmatched += 1
                continue
            metrics.record(p["id"], origin="upload-post", **metric_values(row))
            stored += 1
    return {"snapshots_stored": stored, "posts_unmatched": unmatched}
