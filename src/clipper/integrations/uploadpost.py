"""Upload-Post REST client (https://docs.upload-post.com).

One upload call fans out to every requested platform. Free plan: 10
uploads/month; TikTok needs a paid tier, and TikTok posts from an unaudited
app stay private until TikTok approves it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

BASE = "https://api.upload-post.com/api"


class UploadPostError(RuntimeError):
    pass


class UploadPost:
    def __init__(self, api_key: str, user: str, timeout: float = 300.0):
        if not api_key or not user:
            raise UploadPostError("set UPLOAD_POST_API_KEY and UPLOAD_POST_USER (the Upload-Post profile name)")
        self.user = user
        self.client = httpx.Client(timeout=timeout, headers={"Authorization": f"Apikey {api_key}"})

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code not in (200, 201, 202):
            raise UploadPostError(f"Upload-Post {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def upload(self, video: Path, platforms: list[str], copy: dict[str, dict[str, Any]],
               scheduled_date: str | None = None, timezone: str | None = None,
               tiktok_privacy: str = "PUBLIC_TO_EVERYONE") -> dict[str, Any]:
        def pick(platform: str, *keys: str) -> str | None:
            entry = copy.get(platform) or {}
            return next((entry[k] for k in keys if entry.get(k)), None)

        title = (pick("youtube", "title") or pick("tiktok", "title", "caption")
                 or pick("instagram", "title", "caption") or video.stem)
        data: dict[str, Any] = {"user": self.user, "title": title[:100], "platform[]": platforms,
                                "async_upload": "true"}
        if "tiktok" in platforms:
            data["tiktok_title"] = pick("tiktok", "caption", "title") or title
            data["privacy_level"] = tiktok_privacy
            data["post_mode"] = "DIRECT_POST"
        if "instagram" in platforms:
            data["instagram_title"] = pick("instagram", "caption", "title") or title
            data["media_type"] = "REELS"
        if "youtube" in platforms:
            data["youtube_title"] = (pick("youtube", "title") or title)[:100]
            data["youtube_description"] = pick("youtube", "description", "caption") or ""
            data["privacyStatus"] = "public"
        if scheduled_date:
            data["scheduled_date"] = scheduled_date
            if timezone:
                data["timezone"] = timezone
        with video.open("rb") as fh:
            resp = self.client.post(f"{BASE}/upload", data=data,
                                    files={"video": (video.name, fh, "video/mp4")})
        return self._json(resp)

    def status(self, request_id: str) -> dict[str, Any]:
        return self._json(self.client.get(f"{BASE}/uploadposts/status", params={"request_id": request_id}))

    def post_analytics(self, platform: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user": self.user, "limit": limit}
        if platform:
            params["platform"] = platform
        payload = self._json(self.client.get(f"{BASE}/uploadposts/post-analytics/cached", params=params))
        if isinstance(payload, list):
            return payload
        for key in ("posts", "data", "results", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []


def platform_results(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-platform results from a sync upload or a status poll."""
    results = payload.get("results") or {}
    if isinstance(results, list):  # tolerate a list-of-dicts variant
        results = {r.get("platform"): r for r in results if isinstance(r, dict) and r.get("platform")}
    return {k: v for k, v in results.items() if isinstance(v, dict)}


def metric_values(row: dict[str, Any]) -> dict[str, int | None]:
    m = row.get("post_metrics") or row.get("metrics") or row

    def num(*keys: str) -> int | None:
        for k in keys:
            if m.get(k) is not None:
                try:
                    return int(float(m[k]))
                except (TypeError, ValueError):
                    return None
        return None

    return {"views": num("views", "plays", "video_views", "impressions") or 0,
            "likes": num("likes", "like_count"), "comments": num("comments", "comment_count"),
            "shares": num("shares", "share_count"), "saves": num("saves", "saved", "save_count")}
