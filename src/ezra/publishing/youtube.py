"""YouTube Data API v3 publisher.

- OAuth 2.0 + PKCE, offline access. Scopes: upload, read-only, YouTube Analytics (read-only); the
  broader `youtube.force-ssl` only when editing/deleting is enabled (EZRA_YOUTUBE_MANAGE).
- Resumable uploads in chunks. The session URI is persisted through `PostRequest.on_state`, so a
  retry resumes the same session (or recovers the finished video id) instead of uploading twice.
- Google error reasons are classified: quota exhaustion and invalid input are permanent, rate
  limits and 5xx are retried, revoked or missing access asks for a reconnect.
- Private-only until the Google project passes YouTube's API audit (EZRA_YOUTUBE_PUBLIC_ALLOWED):
  unaudited projects' uploads are locked private, so Ezra refuses public/scheduled uploads rather
  than report a public post that isn't.
- Thumbnails via thumbnails.set after the upload; a thumbnail failure is a warning, never a failed
  upload.

Implemented from Google's public API documentation. vfarcic/youtube-automation (no license) was
read for which scopes and calls a working pipeline uses; no code was copied.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .. import secrets
from ..config import get_settings
from .base import (
    MetricsResult,
    PostRequest,
    Publisher,
    PublishError,
    PublishResult,
    RetryablePublishError,
    expires,
    with_query,
)

log = logging.getLogger("ezra.youtube")

UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
READONLY = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"
MANAGE = "https://www.googleapis.com/auth/youtube.force-ssl"
CHUNK_UNIT = 256 * 1024                  # resumable chunks must be multiples of 256 KiB
THUMB_MAX_BYTES = 2 * 1024 * 1024
TRANSIENT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"}
PERMANENT_REASONS = {
    "quotaExceeded": "quota_exceeded", "dailyLimitExceeded": "quota_exceeded",
    "uploadLimitExceeded": "upload_limit", "invalidTitle": "invalid_metadata",
    "invalidDescription": "invalid_metadata", "invalidTags": "invalid_metadata",
    "invalidCategoryId": "invalid_metadata", "invalidPublishAt": "invalid_schedule",
    "invalidVideoMetadata": "invalid_metadata", "mediaBodyRequired": "invalid_video",
    "invalidFilename": "invalid_video", "videoNotFound": "not_found",
    "forbiddenPrivacySetting": "privacy_not_allowed", "invalidRecordingDetails": "invalid_metadata",
    "insufficientPermissions": "scope_missing", "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "scope_missing",
    "forbidden": "forbidden",
}
RECONNECT_CODES = {"scope_missing", "auth_revoked", "auth_invalid", "refresh_token_missing"}


def google_reason(r: httpx.Response) -> tuple[str | None, str]:
    """The error reason and message from a Google API error body (both shapes: API and OAuth)."""
    try:
        body = r.json()
    except ValueError:
        return None, r.text[:300]
    err = body.get("error")
    if isinstance(err, str):                                   # OAuth token endpoint
        return err, str(body.get("error_description") or err)
    if isinstance(err, dict):
        reasons = [e.get("reason") for e in err.get("errors") or [] if e.get("reason")]
        details = [d.get("reason") for d in err.get("details") or [] if isinstance(d, dict) and d.get("reason")]
        return (reasons or details or [err.get("status")])[0], str(err.get("message") or "")[:300]
    return None, r.text[:300]


def raise_for(r: httpx.Response, what: str) -> None:
    """Map a failed Google response to a structured error."""
    reason, message = google_reason(r)
    detail = {"http_status": r.status_code, "reason": reason, "what": what}
    text = f"YouTube {what}: HTTP {r.status_code} {reason or ''} {message}".strip()
    if r.status_code == 429 or r.status_code >= 500 or reason in TRANSIENT_REASONS:
        raise RetryablePublishError(text, "rate_limited" if r.status_code in (403, 429) else "server_error", detail)
    if reason == "invalid_grant":
        raise PublishError("YouTube refused the stored refresh token (revoked, expired or the consent screen is "
                           "in Testing and the token is over 7 days old): reconnect the account", "auth_revoked",
                           reconnect=True, detail=detail)
    if r.status_code == 401:
        raise PublishError(f"{text}: the access token was rejected; reconnect the account", "auth_invalid",
                           reconnect=True, detail=detail)
    code = PERMANENT_REASONS.get(reason or "", "invalid_request" if r.status_code == 400 else "http_error")
    if code == "quota_exceeded":
        text += " (the daily API quota resets at midnight Pacific time; not retried until then)"
    raise PublishError(text, code, reconnect=code in RECONNECT_CODES, detail=detail)


# --- metadata -----------------------------------------------------------------------------------

def clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"[<>]", "", title or "")).strip()
    if len(t) > 100:
        t = t[:100].rsplit(" ", 1)[0].rstrip(" ,.:;-") or t[:100]
    return t or "Untitled"


def clean_description(text: str) -> str:
    d = re.sub(r"[<>]", "", text or "")
    raw = d.encode("utf-8")
    return raw[:5000].decode("utf-8", "ignore") if len(raw) > 5000 else d


def clean_tags(tags: list[str]) -> list[str]:
    """YouTube counts a tag's length plus a comma between tags, and quotes around tags with spaces;
    the total must stay within 500. Tags are dropped from the end until it fits."""
    out: list[str] = []
    for t in tags:
        t = re.sub(r"[<>,\"]", "", t.lstrip("#")).strip()
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    def total(ts: list[str]) -> int:
        return sum(len(t) + (2 if " " in t else 0) for t in ts) + max(0, len(ts) - 1)
    while out and total(out) > 500:
        out.pop()
    return out


def platform_state(resource: dict[str, Any]) -> str:
    st = resource.get("status") or {}
    upload = st.get("uploadStatus")
    if upload in ("failed", "rejected", "deleted"):
        return upload
    if st.get("publishAt") and st.get("privacyStatus") == "private":
        return "scheduled"
    if upload == "uploaded":
        return "processing"
    return st.get("privacyStatus") or "processing"


class YouTubePublisher(Publisher):
    name = "youtube"
    platforms = ("youtube",)
    supports_scheduling = True
    oauth_authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    revoke_url = "https://oauth2.googleapis.com/revoke"
    upload_url = "https://www.googleapis.com/upload/youtube/v3/videos"
    thumbnail_url = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
    api = "https://www.googleapis.com/youtube/v3"
    oauth_scopes = (UPLOAD, READONLY, ANALYTICS)
    client_secret_ref = "youtube-client"
    max_chunk_attempts = 5
    sleep = staticmethod(time.sleep)            # backoff between chunk retries; tests replace it

    # --- OAuth ------------------------------------------------------------------------------
    def scopes(self, manage: bool | None = None) -> tuple[str, ...]:
        manage = get_settings().youtube_manage if manage is None else manage
        return (*self.oauth_scopes, MANAGE) if manage else self.oauth_scopes

    def authorize_url(self, state: str, redirect_uri: str, code_challenge: str, manage: bool | None = None) -> str:
        c = self.client_credentials()
        return with_query(self.oauth_authorize_url, {
            "client_id": c["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(self.scopes(manage)), "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true", "state": state, "code_challenge": code_challenge,
            "code_challenge_method": "S256"})

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict[str, Any]:
        c = self.client_credentials()
        r = self.http.post(self.token_url, data={
            "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": code_verifier})
        if r.status_code >= 400:
            raise_for(r, "token exchange")
        tok = expires(r.json())
        tok["scopes"] = (tok.get("scope") or "").split()
        if UPLOAD not in tok["scopes"] and tok["scopes"]:
            raise PublishError("the upload permission was not granted on the consent screen; connect again and "
                               "tick every box", "scope_missing", reconnect=True)
        if not tok.get("refresh_token"):
            # without it Ezra can't post once the hour-long access token expires
            raise PublishError("Google returned no refresh token. Remove Ezra's access at "
                               "https://myaccount.google.com/permissions and connect again", "refresh_token_missing",
                               reconnect=True)
        ch = self.http.get(f"{self.api}/channels", params={"part": "snippet", "mine": "true"},
                           headers={"Authorization": f"Bearer {tok['access_token']}"})
        if ch.status_code >= 400:
            raise_for(ch, "channel lookup")
        items = ch.json().get("items") or []
        if not items:
            raise PublishError("this Google account has no YouTube channel; create one, then connect again",
                               "no_channel")
        tok["account"] = items[0]["snippet"]["title"]
        tok["channel_id"] = items[0]["id"]
        log.info("youtube connected", extra={"channel_id": tok["channel_id"], "scopes": tok["scopes"]})
        return tok

    def refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        if not token.get("refresh_token"):
            raise PublishError("no refresh token stored; reconnect the account", "refresh_token_missing",
                               reconnect=True)
        c = self.client_credentials()
        r = self.http.post(self.token_url, data={
            "client_id": c["client_id"], "client_secret": c["client_secret"],
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code >= 400:
            raise_for(r, "token refresh")
        log.info("youtube token refreshed")
        return expires({**token, **r.json(), "expires_at": None})   # Google keeps the refresh token

    def revoke(self, token: dict[str, Any]) -> bool:
        t = token.get("refresh_token") or token.get("access_token")
        if not t:
            return False
        r = self.http.post(self.revoke_url, params={"token": t},
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        return r.status_code == 200

    def _call(self, account: dict[str, Any], method: str, url: str, what: str, **kw: Any) -> httpx.Response:
        """An authorized API call; a 401 forces one token refresh (the token can be revoked early)."""
        for attempt in (0, 1):
            tok = self.token(account)
            if attempt:
                tok = self.refresh(tok)
                secrets.put(account["credential_ref"], tok)
            headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {tok['access_token']}"}
            try:
                r = self.http.request(method, url, headers=headers, **kw)
            except httpx.TransportError as e:
                raise RetryablePublishError(f"YouTube {what}: {type(e).__name__}", "connection") from e
            if r.status_code == 401 and attempt == 0:
                kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                continue
            if r.status_code >= 400:
                raise_for(r, what)
            return r
        raise AssertionError("unreachable")

    # --- upload -------------------------------------------------------------------------------
    def check_visibility(self, req: PostRequest) -> None:
        public = req.visibility == "public"
        if (public or req.scheduled_at) and not get_settings().youtube_public_allowed:
            raise PublishError(
                "YouTube uploads are private-only until the Google API project passes YouTube's audit "
                "(unaudited projects' uploads are locked private). Upload as private, or set "
                "EZRA_YOUTUBE_PUBLIC_ALLOWED=1 once the audit is approved.", "private_only")
        if req.scheduled_at and req.scheduled_at.astimezone(UTC) <= datetime.now(UTC):
            raise PublishError(f"publish time {req.scheduled_at.isoformat()} is not in the future", "invalid_schedule")

    def body(self, req: PostRequest) -> dict[str, Any]:
        status: dict[str, Any] = {"privacyStatus": req.visibility if req.visibility in ("private", "unlisted")
                                  else "public", "selfDeclaredMadeForKids": False}
        if req.scheduled_at:
            # scheduled = uploaded private with publishAt; YouTube flips it public at that time
            status.update(privacyStatus="private", publishAt=_rfc3339(req.scheduled_at))
        return {"snippet": {"title": clean_title(req.title or req.caption),
                            "description": clean_description(req.description or req.caption),
                            "tags": clean_tags(req.hashtags), "categoryId": get_settings().youtube_category_id},
                "status": status}

    def publish(self, account: dict[str, Any], req: PostRequest, platform: str) -> PublishResult:
        self.check_visibility(req)
        state = dict(req.upload_state or {})

        def save(**changes: Any) -> None:
            state.update(changes)
            if req.on_state:
                req.on_state(dict(state))

        if state.get("video_id"):                      # finished in an earlier attempt: never upload twice
            resource = state.get("resource") or {"id": state["video_id"]}
            return self._result(req, resource, account, recovered=True)
        size = req.video.stat().st_size
        resource = None
        offset = 0
        if state.get("session_uri") and state.get("size") == size:
            done, offset = self._query(account, state["session_uri"], size)
            if done is not None:
                resource = done
            elif offset < 0:                           # the session expired: start again
                state.pop("session_uri", None)
                offset = 0
        if resource is None and not state.get("session_uri"):
            init = self._call(account, "POST", with_query(self.upload_url, {"uploadType": "resumable",
                                                                            "part": "snippet,status"}),
                              "upload init", json=self.body(req),
                              headers={"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
            location = init.headers.get("Location") or init.headers.get("location")
            if not location:
                raise PublishError("YouTube did not return an upload session URL", "upload_session")
            save(session_uri=location, size=size, started_at=datetime.now(UTC).isoformat())
            log.info("youtube upload started", extra={"bytes": size})
        if resource is None:
            resource = self._upload(account, state["session_uri"], req, size, offset, save)
        vid = resource.get("id")
        if not vid:
            raise PublishError(f"YouTube upload returned no video id: {str(resource)[:300]}", "upload_incomplete")
        save(video_id=vid, resource={k: resource.get(k) for k in ("id", "status", "snippet")}, bytes_sent=size)
        log.info("youtube upload complete", extra={"video_id": vid})
        return self._result(req, resource, account)

    def _chunk_size(self) -> int:
        return max(1, get_settings().youtube_chunk_mb * 1024 * 1024 // CHUNK_UNIT) * CHUNK_UNIT

    def _query(self, account: dict[str, Any], session: str, size: int) -> tuple[dict[str, Any] | None, int]:
        """Where an interrupted session stands: (finished resource, _) or (None, next byte), or
        (None, -1) when the session is gone."""
        r = self._raw_put(account, session, b"", {"Content-Range": f"bytes */{size}"}, "upload status")
        if r.status_code in (200, 201):
            return r.json(), size
        if r.status_code == 308:
            return None, _next_byte(r)
        if r.status_code in (404, 410):
            return None, -1
        raise_for(r, "upload status")
        return None, 0

    def _raw_put(self, account: dict[str, Any], url: str, data: bytes, headers: dict[str, str],
                 what: str) -> httpx.Response:
        tok = self.token(account)
        try:
            return self.http.put(url, content=data, headers={"Authorization": f"Bearer {tok['access_token']}",
                                                             **headers})
        except httpx.TransportError as e:
            raise RetryablePublishError(f"YouTube {what}: {type(e).__name__}", "connection") from e

    def _upload(self, account: dict[str, Any], session: str, req: PostRequest, size: int, offset: int,
                save: Any) -> dict[str, Any]:
        """Send the file in chunks from `offset`. Memory use is one chunk. A transient failure
        re-asks the session where it stands and resends from there, with backoff."""
        chunk = self._chunk_size()
        failures = 0
        with req.video.open("rb") as fh:
            while True:
                fh.seek(offset)
                data = fh.read(chunk)
                end = offset + len(data) - 1
                headers = {"Content-Type": "video/mp4",
                           "Content-Range": f"bytes {offset}-{end}/{size}" if data else f"bytes */{size}"}
                try:
                    r = self._raw_put(account, session, data, headers, "upload")
                    if r.status_code == 429 or r.status_code >= 500:
                        raise_for(r, "upload")
                except RetryablePublishError as e:
                    failures += 1
                    log.warning("youtube chunk failed", extra={"offset": offset, "attempt": failures, "code": e.code})
                    if failures >= self.max_chunk_attempts:
                        raise                          # the job retries later and resumes this session
                    self.sleep(min(30.0, 2.0 ** failures))
                    done, offset = self._query(account, session, size)
                    if done is not None:
                        return done
                    if offset < 0:
                        raise RetryablePublishError("YouTube upload session expired", "upload_session") from e
                    continue
                if r.status_code in (200, 201):
                    if req.on_progress:
                        req.on_progress(size, size)
                    return r.json()
                if r.status_code == 308:
                    offset = _next_byte(r)
                    failures = 0
                    save(bytes_sent=offset)
                    if req.on_progress:
                        req.on_progress(offset, size)
                    continue
                raise_for(r, "upload")

    def _result(self, req: PostRequest, resource: dict[str, Any], account: dict[str, Any],
                recovered: bool = False) -> PublishResult:
        vid = resource["id"]
        warnings: list[dict[str, Any]] = []
        st = resource.get("status") or {}
        if req.visibility == "public" and not req.scheduled_at and st.get("privacyStatus") == "private":
            warnings.append({"code": "locked_private", "message": "YouTube kept the video private (an unaudited "
                             "API project can only upload private videos)"})
        if req.thumbnail and not recovered:
            outcome = self.upload_thumbnail(account, vid, req.thumbnail)
            if outcome["status"] != "set":
                warnings.append({"code": "thumbnail_" + outcome["status"], "message": outcome["message"]})
        state = platform_state(resource) if st else "processing"
        return PublishResult("scheduled" if req.scheduled_at else "published", vid,
                             f"https://www.youtube.com/shorts/{vid}", {"id": vid, "status": st, "recovered": recovered},
                             platform_state=state, warnings=warnings)

    # --- thumbnail ------------------------------------------------------------------------------
    def upload_thumbnail(self, account: dict[str, Any], video_id: str, path: Path) -> dict[str, Any]:
        """Never raises: the thumbnail is optional, so its outcome is reported instead."""
        try:
            data, mime = prepare_thumbnail(path)
        except ValueError as e:
            return {"status": "invalid", "message": str(e)}
        try:
            self._call(account, "POST", with_query(self.thumbnail_url, {"videoId": video_id}), "thumbnail",
                       content=data, headers={"Content-Type": mime})
        except (PublishError, RetryablePublishError) as e:
            msg = str(e)
            if getattr(e, "code", "") == "forbidden":
                msg += " (custom thumbnails need a verified channel: youtube.com/verify)"
            log.warning("youtube thumbnail failed", extra={"video_id": video_id, "code": getattr(e, "code", "")})
            return {"status": "failed", "message": msg[:500]}
        log.info("youtube thumbnail set", extra={"video_id": video_id})
        return {"status": "set", "message": "thumbnail set"}

    # --- lifecycle --------------------------------------------------------------------------------
    def video(self, account: dict[str, Any], video_id: str) -> dict[str, Any] | None:
        r = self._call(account, "GET", f"{self.api}/videos", "video lookup",
                       params={"part": "snippet,status,processingDetails", "id": video_id})
        items = r.json().get("items") or []
        return items[0] if items else None

    def refresh_status(self, account: dict[str, Any], external_id: str) -> PublishResult | None:
        v = self.video(account, external_id)
        if v is None:
            return PublishResult("deleted", external_id, None, {}, platform_state="deleted")
        state = platform_state(v)
        status = "published" if state in ("public", "unlisted", "private", "processing") else state
        return PublishResult(status, external_id, f"https://www.youtube.com/shorts/{external_id}",
                             {"status": v.get("status"), "processingDetails": v.get("processingDetails")},
                             platform_state=state)

    def update_video(self, account: dict[str, Any], video_id: str, *, title: str | None = None,
                     description: str | None = None, tags: list[str] | None = None, privacy: str | None = None,
                     publish_at: datetime | str | None = "unchanged") -> dict[str, Any]:
        """Edit an uploaded video. Needs the youtube.force-ssl scope (EZRA_YOUTUBE_MANAGE=1 at connect).
        videos.update replaces whole parts, so the current snippet and status are read first."""
        self._require_manage(account)
        v = self.video(account, video_id)
        if v is None:
            raise PublishError(f"video {video_id} no longer exists on YouTube", "not_found")
        snippet = {k: v["snippet"].get(k) for k in ("title", "description", "tags", "categoryId")
                   if v["snippet"].get(k) is not None}
        status = {k: v["status"].get(k) for k in ("privacyStatus", "publishAt", "selfDeclaredMadeForKids")
                  if v["status"].get(k) is not None}
        if title is not None:
            snippet["title"] = clean_title(title)
        if description is not None:
            snippet["description"] = clean_description(description)
        if tags is not None:
            snippet["tags"] = clean_tags(tags)
        if privacy is not None:
            if privacy == "public" and not get_settings().youtube_public_allowed:
                raise PublishError("public videos need an audited API project (EZRA_YOUTUBE_PUBLIC_ALLOWED)",
                                   "private_only")
            status["privacyStatus"] = privacy
        if publish_at != "unchanged":
            if publish_at is None:
                status.pop("publishAt", None)          # cancel the schedule: stays private
            else:
                when = datetime.fromisoformat(publish_at) if isinstance(publish_at, str) else publish_at
                if when.astimezone(UTC) <= datetime.now(UTC):
                    raise PublishError("the new publish time is not in the future", "invalid_schedule")
                status.update(privacyStatus="private", publishAt=_rfc3339(when))
        snippet.setdefault("categoryId", get_settings().youtube_category_id)
        r = self._call(account, "PUT", f"{self.api}/videos", "update", params={"part": "snippet,status"},
                       json={"id": video_id, "snippet": snippet, "status": status})
        log.info("youtube video updated", extra={"video_id": video_id})
        return r.json()

    def delete_video(self, account: dict[str, Any], video_id: str) -> None:
        self._require_manage(account)
        self._call(account, "DELETE", f"{self.api}/videos", "delete", params={"id": video_id})
        log.info("youtube video deleted", extra={"video_id": video_id})

    def _require_manage(self, account: dict[str, Any]) -> None:
        scopes = (account.get("meta") or {}).get("scopes") or []
        if scopes and MANAGE not in scopes:
            raise PublishError("editing or deleting YouTube videos needs the youtube.force-ssl permission: set "
                               "EZRA_YOUTUBE_MANAGE=1 and reconnect", "scope_missing", reconnect=True)

    def metrics(self, account: dict[str, Any], external_id: str) -> MetricsResult | None:
        r = self._call(account, "GET", f"{self.api}/videos", "statistics",
                       params={"part": "statistics", "id": external_id})
        items = r.json().get("items") or []
        if not items:
            return None
        st = items[0].get("statistics", {})
        return MetricsResult(views=_int(st.get("viewCount")), likes=_int(st.get("likeCount")),
                             comments=_int(st.get("commentCount")), raw=st)

    def channel(self, account: dict[str, Any]) -> dict[str, Any] | None:
        r = self._call(account, "GET", f"{self.api}/channels", "channel lookup",
                       params={"part": "snippet,status", "mine": "true"})
        items = r.json().get("items") or []
        return items[0] if items else None


def _rfc3339(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _next_byte(r: httpx.Response) -> int:
    rng = r.headers.get("Range") or r.headers.get("range")
    m = re.match(r"bytes=0-(\d+)", rng or "")
    return int(m.group(1)) + 1 if m else 0


def prepare_thumbnail(path: Path) -> tuple[bytes, str]:
    """A JPEG or PNG under YouTube's 2 MB limit. Oversized or odd images are re-encoded as JPEG
    (long side 1280 px); anything unreadable is rejected with a reason."""
    from PIL import Image, UnidentifiedImageError

    if not path.exists():
        raise ValueError(f"thumbnail {path.name} not found")
    raw = path.read_bytes()
    kind = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else None
    try:
        img = Image.open(path)
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError(f"thumbnail {path.name} is not a readable image") from e
    w, h = img.size
    if min(w, h) < 120:
        raise ValueError(f"thumbnail {path.name} is too small ({w}x{h})")
    if kind and len(raw) <= THUMB_MAX_BYTES:
        return raw, kind
    import io

    rgb = img.convert("RGB")
    rgb.thumbnail((1280, 1280))
    for quality in (90, 80, 70, 60):
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=quality)
        if buf.tell() <= THUMB_MAX_BYTES:
            return buf.getvalue(), "image/jpeg"
    raise ValueError(f"thumbnail {path.name} can't be brought under 2 MB")


def _int(v: Any) -> int | None:
    try:
        return None if v is None else int(float(v))
    except (TypeError, ValueError):
        return None
