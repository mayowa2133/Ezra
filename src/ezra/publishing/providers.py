"""Publishing adapters.

youtube       YouTube Data API v3: resumable upload, privacyStatus / publishAt
              scheduling, statistics for metrics. OAuth 2.0 + PKCE.
tiktok        TikTok Content Posting API (Direct Post, FILE_UPLOAD chunks),
              status fetch, video/query metrics. Unaudited apps post SELF_ONLY.
instagram     Instagram API with Instagram Login: REELS container with
              resumable upload to rupload.facebook.com, status poll,
              media_publish, media insights.
upload-post   Optional third-party aggregator (upload-post.com). Not required.
local-export  Writes the post (video + metadata) to a folder for manual
              upload. Real, credential-free, and what the demo uses.
"""

from __future__ import annotations

import json

import re
import shutil
import time
from datetime import timezone
from pathlib import Path
from typing import Any

import httpx

from .. import secrets
from ..config import get_settings
from .base import (MetricsResult, PostRequest, Publisher, PublishError, PublishResult, expires, with_query)


class YouTubePublisher(Publisher):
    name = "youtube"
    platforms = ("youtube",)
    supports_scheduling = True
    oauth_authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    upload_url = "https://www.googleapis.com/upload/youtube/v3/videos"
    api = "https://www.googleapis.com/youtube/v3"
    oauth_scopes = ("https://www.googleapis.com/auth/youtube.upload",
                    "https://www.googleapis.com/auth/youtube.readonly")
    client_secret_ref = "youtube-client"

    def authorize_url(self, state: str, redirect_uri: str, code_challenge: str) -> str:
        c = self.client_credentials()
        return with_query(self.oauth_authorize_url, {
            "client_id": c["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(self.oauth_scopes), "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256"})

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict[str, Any]:
        c = self.client_credentials()
        tok = self._check(self.http.post(self.token_url, data={
            "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": code_verifier}),
            "token exchange")
        tok = expires(tok)
        ch = self._check(self.http.get(f"{self.api}/channels", params={"part": "snippet", "mine": "true"},
                                       headers={"Authorization": f"Bearer {tok['access_token']}"}), "channel lookup")
        items = ch.get("items") or []
        tok["account"] = items[0]["snippet"]["title"] if items else "youtube"
        tok["channel_id"] = items[0]["id"] if items else None
        return tok

    def refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        c = self.client_credentials()
        new = self._check(self.http.post(self.token_url, data={
            "client_id": c["client_id"], "client_secret": c["client_secret"],
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}), "token refresh")
        return expires({**token, **new, "expires_at": None})

    def publish(self, account: dict[str, Any], req: PostRequest, platform: str) -> PublishResult:
        tok = self.token(account)
        status: dict[str, Any] = {"privacyStatus": "private" if req.visibility == "private" else
                                  "unlisted" if req.visibility == "unlisted" else "public",
                                  "selfDeclaredMadeForKids": False}
        if req.scheduled_at and req.visibility == "public":
            status.update(privacyStatus="private", publishAt=req.scheduled_at.astimezone(timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        body = {"snippet": {"title": (req.title or req.caption)[:100], "description": req.description or req.caption,
                            "tags": [h.lstrip("#") for h in req.hashtags][:15], "categoryId": "22"},
                "status": status}
        size = req.video.stat().st_size
        headers = {"Authorization": f"Bearer {tok['access_token']}", "X-Upload-Content-Type": "video/mp4",
                   "X-Upload-Content-Length": str(size), "Content-Type": "application/json; charset=UTF-8"}
        init = self.http.post(with_query(self.upload_url, {"uploadType": "resumable", "part": "snippet,status"}),
                              headers=headers, content=json.dumps(body))
        self._check(init, "upload init")
        location = init.headers.get("Location") or init.headers.get("location")
        if not location:
            raise PublishError("YouTube did not return an upload session URL")
        with req.video.open("rb") as fh:
            done = self._check(self.http.put(location, content=fh.read(), headers={
                "Authorization": f"Bearer {tok['access_token']}", "Content-Type": "video/mp4"}), "upload")
        vid = done.get("id")
        if not vid:
            raise PublishError(f"YouTube upload returned no video id: {str(done)[:300]}")
        return PublishResult("scheduled" if "publishAt" in status else "published", vid,
                             f"https://www.youtube.com/shorts/{vid}", done)

    def metrics(self, account: dict[str, Any], external_id: str) -> MetricsResult | None:
        tok = self.token(account)
        d = self._check(self.http.get(f"{self.api}/videos", params={"part": "statistics", "id": external_id},
                                      headers={"Authorization": f"Bearer {tok['access_token']}"}), "statistics")
        items = d.get("items") or []
        if not items:
            return None
        st = items[0].get("statistics", {})
        return MetricsResult(views=_int(st.get("viewCount")), likes=_int(st.get("likeCount")),
                             comments=_int(st.get("commentCount")), raw=st)


class TikTokPublisher(Publisher):
    name = "tiktok"
    platforms = ("tiktok",)
    oauth_authorize_url = "https://www.tiktok.com/v2/auth/authorize/"
    api = "https://open.tiktokapis.com/v2"
    oauth_scopes = ("user.info.basic", "video.publish", "video.list")
    client_secret_ref = "tiktok-client"
    MIN_CHUNK = 5 * 1024 * 1024
    CHUNK = 10 * 1024 * 1024

    def authorize_url(self, state: str, redirect_uri: str, code_challenge: str) -> str:
        c = self.client_credentials()
        return with_query(self.oauth_authorize_url, {
            "client_key": c["client_id"], "scope": ",".join(self.oauth_scopes), "response_type": "code",
            "redirect_uri": redirect_uri, "state": state, "code_challenge": code_challenge,
            "code_challenge_method": "S256"})

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict[str, Any]:
        c = self.client_credentials()
        tok = self._check(self.http.post(f"{self.api}/oauth/token/", data={
            "client_key": c["client_id"], "client_secret": c["client_secret"], "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code_verifier": code_verifier}),
            "token exchange")
        tok = expires(tok)
        me = self._check(self.http.get(f"{self.api}/user/info/", params={"fields": "open_id,display_name"},
                                       headers={"Authorization": f"Bearer {tok['access_token']}"}), "user info")
        tok["account"] = ((me.get("data") or {}).get("user") or {}).get("display_name") or tok.get("open_id")
        return tok

    def refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        c = self.client_credentials()
        new = self._check(self.http.post(f"{self.api}/oauth/token/", data={
            "client_key": c["client_id"], "client_secret": c["client_secret"], "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"]}), "token refresh")
        return expires({**token, **new, "expires_at": None})

    def publish(self, account: dict[str, Any], req: PostRequest, platform: str) -> PublishResult:
        tok = self.token(account)
        auth = {"Authorization": f"Bearer {tok['access_token']}", "Content-Type": "application/json; charset=UTF-8"}
        size = req.video.stat().st_size
        chunk = size if size < self.MIN_CHUNK else self.CHUNK
        count = max(1, size // chunk) if size >= self.MIN_CHUNK else 1
        privacy = "SELF_ONLY" if req.visibility != "public" or account.get("meta", {}).get("unaudited", True) \
            else "PUBLIC_TO_EVERYONE"
        init = self._check(self.http.post(f"{self.api}/post/publish/video/init/", headers=auth, json={
            "post_info": {"title": req.caption[:2200], "privacy_level": privacy, "disable_duet": False,
                          "disable_stitch": False, "disable_comment": False},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                            "total_chunk_count": count}}), "init")
        data = init.get("data") or {}
        if (init.get("error") or {}).get("code", "ok") != "ok":
            raise PublishError(f"TikTok init error: {init['error']}")
        upload_url, publish_id = data.get("upload_url"), data.get("publish_id")
        if not upload_url or not publish_id:
            raise PublishError(f"TikTok init returned no upload_url/publish_id: {init}")
        with req.video.open("rb") as fh:
            for i in range(count):
                first = i * chunk
                last = size - 1 if i == count - 1 else first + chunk - 1  # last chunk absorbs the remainder
                fh.seek(first)
                body = fh.read(last - first + 1)
                self._check(self.http.put(upload_url, content=body, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {first}-{last}/{size}"}), f"chunk {i + 1}/{count}")
        res = self.refresh_status(account, publish_id)
        return res or PublishResult("processing", publish_id, None, {"privacy_level": privacy})

    def refresh_status(self, account: dict[str, Any], external_id: str) -> PublishResult | None:
        tok = self.token(account)
        d = self._check(self.http.post(f"{self.api}/post/publish/status/fetch/", json={"publish_id": external_id},
                                       headers={"Authorization": f"Bearer {tok['access_token']}",
                                                "Content-Type": "application/json; charset=UTF-8"}), "status")
        data = d.get("data") or {}
        st = data.get("status")
        if st == "FAILED":
            raise PublishError(f"TikTok publish failed: {data.get('fail_reason')}")
        ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
        if st == "PUBLISH_COMPLETE":
            vid = str(ids[0]) if ids else external_id
            return PublishResult("published", vid, f"https://www.tiktok.com/video/{vid}" if ids else None, data)
        return PublishResult("processing", external_id, None, data)

    def metrics(self, account: dict[str, Any], external_id: str) -> MetricsResult | None:
        tok = self.token(account)
        d = self._check(self.http.post(
            with_query(f"{self.api}/video/query/", {"fields": "id,view_count,like_count,comment_count,share_count"}),
            json={"filters": {"video_ids": [external_id]}},
            headers={"Authorization": f"Bearer {tok['access_token']}", "Content-Type": "application/json"}), "query")
        vids = (d.get("data") or {}).get("videos") or []
        if not vids:
            return None
        v = vids[0]
        return MetricsResult(views=_int(v.get("view_count")), likes=_int(v.get("like_count")),
                             comments=_int(v.get("comment_count")), shares=_int(v.get("share_count")), raw=v)


class InstagramPublisher(Publisher):
    name = "instagram"
    platforms = ("instagram",)
    supports_private = False
    oauth_authorize_url = "https://www.instagram.com/oauth/authorize"
    graph = "https://graph.instagram.com/v25.0"
    rupload = "https://rupload.facebook.com/ig-api-upload/v25.0"
    oauth_scopes = ("instagram_business_basic", "instagram_business_content_publish",
                    "instagram_business_manage_insights")
    client_secret_ref = "instagram-client"
    poll_seconds = 10.0
    poll_limit = 30

    def authorize_url(self, state: str, redirect_uri: str, code_challenge: str) -> str:
        c = self.client_credentials()
        return with_query(self.oauth_authorize_url, {"client_id": c["client_id"], "redirect_uri": redirect_uri,
                                                     "response_type": "code", "scope": ",".join(self.oauth_scopes),
                                                     "state": state})

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict[str, Any]:
        c = self.client_credentials()
        short = self._check(self.http.post("https://api.instagram.com/oauth/access_token", data={
            "client_id": c["client_id"], "client_secret": c["client_secret"], "grant_type": "authorization_code",
            "redirect_uri": redirect_uri, "code": code}), "token exchange")
        long = self._check(self.http.get("https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": c["client_secret"],
            "access_token": short["access_token"]}), "long-lived token")
        tok = expires({**long, "user_id": str(short.get("user_id"))})
        me = self._check(self.http.get(f"{self.graph}/me", params={"fields": "user_id,username",
                                                                   "access_token": tok["access_token"]}), "me")
        tok["account"] = me.get("username") or tok["user_id"]
        tok["ig_user_id"] = str(me.get("user_id") or tok["user_id"])
        return tok

    def refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        new = self._check(self.http.get("https://graph.instagram.com/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": token["access_token"]}), "token refresh")
        return expires({**token, **new, "expires_at": None})

    def publish(self, account: dict[str, Any], req: PostRequest, platform: str) -> PublishResult:
        if req.visibility != "public":
            raise PublishError("Instagram has no private posts; publish publicly or leave Instagram out")
        tok = self.token(account)
        ig = tok.get("ig_user_id") or account.get("meta", {}).get("ig_user_id")
        if not ig:
            raise PublishError("connected Instagram account has no ig_user_id")
        at = tok["access_token"]
        cont = self._check(self.http.post(f"{self.graph}/{ig}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": req.caption[:2200],
            "access_token": at}), "create container")
        cid = cont.get("id")
        if not cid:
            raise PublishError(f"no container id: {cont}")
        size = req.video.stat().st_size
        with req.video.open("rb") as fh:
            self._check(self.http.post(f"{self.rupload}/{cid}", content=fh.read(), headers={
                "Authorization": f"OAuth {at}", "offset": "0", "file_size": str(size)}), "upload")
        for _ in range(self.poll_limit):
            st = self._check(self.http.get(f"{self.graph}/{cid}", params={"fields": "status_code",
                                                                          "access_token": at}), "status")
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram processing {code}: {st}")
            time.sleep(self.poll_seconds)
        else:
            raise PublishError("Instagram container still processing after polling; retry later")
        pub = self._check(self.http.post(f"{self.graph}/{ig}/media_publish", data={
            "creation_id": cid, "access_token": at}), "media_publish")
        mid = pub.get("id")
        link = self._check(self.http.get(f"{self.graph}/{mid}", params={"fields": "permalink",
                                                                        "access_token": at}), "permalink")
        return PublishResult("published", mid, link.get("permalink"), pub)

    def metrics(self, account: dict[str, Any], external_id: str) -> MetricsResult | None:
        tok = self.token(account)
        d = self._check(self.http.get(f"{self.graph}/{external_id}/insights", params={
            "metric": "views,likes,comments,shares,saved,reach", "access_token": tok["access_token"]}), "insights")
        vals = {m["name"]: (m.get("values") or [{}])[0].get("value") for m in d.get("data", [])}
        return MetricsResult(views=_int(vals.get("views")), likes=_int(vals.get("likes")),
                             comments=_int(vals.get("comments")), shares=_int(vals.get("shares")),
                             saves=_int(vals.get("saved")), impressions=_int(vals.get("reach")), raw=vals)


class UploadPostPublisher(Publisher):
    """Optional aggregator. Ezra does not depend on it architecturally."""
    name = "upload-post"
    platforms = ("tiktok", "instagram", "youtube", "x", "linkedin", "facebook", "threads")
    supports_scheduling = True
    api = "https://api.upload-post.com/api"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Apikey {secrets.require('upload-post', 'Upload-Post publishing')}"}

    def publish(self, account: dict[str, Any], req: PostRequest, platform: str) -> PublishResult:
        data: dict[str, Any] = {"user": account["handle"], "title": (req.title or req.caption)[:100],
                                "platform[]": [platform], "async_upload": "true"}
        if platform == "tiktok":
            data.update(tiktok_title=req.caption, privacy_level="SELF_ONLY" if req.visibility != "public"
                        else "PUBLIC_TO_EVERYONE", post_mode="DIRECT_POST")
        elif platform == "instagram":
            if req.visibility != "public":
                raise PublishError("Instagram has no private posts")
            data.update(instagram_title=req.caption, media_type="REELS")
        elif platform == "youtube":
            data.update(youtube_title=(req.title or req.caption)[:100], youtube_description=req.description or "",
                        privacyStatus="private" if req.visibility == "private" else "public")
        if req.scheduled_at:
            data["scheduled_date"] = req.scheduled_at.astimezone(timezone.utc).isoformat()
        with req.video.open("rb") as fh:
            d = self._check(self.http.post(f"{self.api}/upload", headers=self._headers(), data=data,
                                           files={"video": (req.video.name, fh, "video/mp4")}), "upload")
        r = (d.get("results") or {}).get(platform) or {}
        if r.get("success") is False:
            raise PublishError(f"Upload-Post {platform}: {r.get('error')}")
        if r.get("url") or r.get("post_id"):
            return PublishResult("published", str(r.get("post_id") or ""), r.get("url"), d)
        return PublishResult("scheduled" if req.scheduled_at else "processing",
                             d.get("request_id") or d.get("job_id"), None, d)

    def refresh_status(self, account: dict[str, Any], external_id: str) -> PublishResult | None:
        d = self._check(self.http.get(f"{self.api}/uploadposts/status", params={"request_id": external_id},
                                      headers=self._headers()), "status")
        for plat, r in (d.get("results") or {}).items():
            if isinstance(r, dict) and (r.get("url") or r.get("post_id")):
                return PublishResult("published", str(r.get("post_id") or external_id), r.get("url"), r)
            if isinstance(r, dict) and r.get("success") is False:
                raise PublishError(f"Upload-Post {plat}: {r.get('error')}")
        return None


class LocalExportPublisher(Publisher):
    """Publishes to a folder (video, thumbnail, caption.txt, post.json) for manual
    upload. Credential-free; the default target for demos and dry runs."""
    name = "local-export"
    platforms = ("tiktok", "instagram", "youtube", "x", "linkedin", "facebook", "threads")
    supports_scheduling = True

    def publish(self, account: dict[str, Any], req: PostRequest, platform: str) -> PublishResult:
        root = Path(account.get("meta", {}).get("dir") or get_settings().home / "published")
        slug = re.sub(r"[^a-z0-9]+", "-", (req.title or req.caption).lower()).strip("-")[:50] or "post"
        dest = root / platform / f"{int(time.time() * 1000)}-{slug}"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(req.video, dest / "video.mp4")
        if req.thumbnail and req.thumbnail.exists():
            shutil.copyfile(req.thumbnail, dest / "thumbnail.jpg")
        (dest / "caption.txt").write_text(req.caption)
        (dest / "post.json").write_text(json.dumps({
            "platform": platform, "title": req.title, "caption": req.caption, "description": req.description,
            "hashtags": req.hashtags, "visibility": req.visibility,
            "scheduled_at": req.scheduled_at.isoformat() if req.scheduled_at else None}, indent=2))
        return PublishResult("published", dest.name, dest.resolve().as_uri(), {"dir": str(dest)})


def _int(v: Any) -> int | None:
    try:
        return None if v is None else int(float(v))
    except (TypeError, ValueError):
        return None


PUBLISHERS: dict[str, type[Publisher]] = {p.name: p for p in (
    YouTubePublisher, TikTokPublisher, InstagramPublisher, UploadPostPublisher, LocalExportPublisher)}


def get_publisher(name: str, client: httpx.Client | None = None) -> Publisher:
    if name not in PUBLISHERS:
        raise ValueError(f"unknown publishing provider {name!r}; available: {sorted(PUBLISHERS)}")
    return PUBLISHERS[name](client)


__all__ = ["PUBLISHERS", "get_publisher", "PublishError"]
