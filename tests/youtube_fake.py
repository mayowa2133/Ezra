"""A stateful stand-in for the YouTube Data API, OAuth token endpoint and thumbnails endpoint,
following Google's documented request/response shapes: resumable sessions that track received
bytes, 308 + Range while incomplete, status queries with `Content-Range: bytes */N`, error bodies
with `error.errors[].reason`, and `invalid_grant` from the token endpoint.

Knobs let tests inject the real-world failures: lost responses, dropped connections, 5xx on a
chunk, quota exhaustion, revoked tokens, expired sessions, unverified channels."""

from __future__ import annotations

import itertools
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

UPLOAD = "https://www.googleapis.com/auth/youtube.upload"


def gerr(status: int, reason: str, message: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message or reason,
                                                  "errors": [{"reason": reason, "message": message or reason}]}})


class FakeYouTube:
    def __init__(self) -> None:
        self.valid_tokens = {"at-1"}
        self.revoked = False
        self.scope = " ".join([UPLOAD, "https://www.googleapis.com/auth/youtube.readonly",
                               "https://www.googleapis.com/auth/yt-analytics.readonly"])
        self.return_refresh_token = True
        self.channels = [{"id": "UC123", "snippet": {"title": "Ezra Test"}, "status": {"isLinked": True}}]
        self.sessions: dict[str, dict[str, Any]] = {}
        self.videos: dict[str, dict[str, Any]] = {}
        self.thumbnails: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.put_ranges: list[str] = []
        self.init_bodies: list[dict[str, Any]] = []
        self.fail_puts: list[int] = []            # statuses to return for the next data PUTs
        self.drop_puts: list[int] = []            # data PUT numbers (1-based) whose connection drops
        self.lose_final_response = False          # finish the upload, then drop the connection
        self.expire_sessions = False              # status queries answer 404
        self.init_error: httpx.Response | None = None
        self.thumbnail_error: httpx.Response | None = None
        self.api_401_once = False
        self.privacy_override: str | None = None  # e.g. "private": what an unaudited project gets
        self.revoke_calls = 0
        self.analytics: dict[str, dict[str, Any]] = {}   # video id -> metrics row
        self.analytics_requests: list[dict[str, list[str]]] = []
        self._ids = itertools.count(1)
        self._data_puts = 0

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    # --- helpers --------------------------------------------------------------------------------
    def _authorized(self, r: httpx.Request) -> bool:
        return r.headers.get("Authorization", "").removeprefix("Bearer ") in self.valid_tokens

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.requests.append(r)
        url = str(r.url)
        if "oauth2.googleapis.com/token" in url:
            return self._token(r)
        if "oauth2.googleapis.com/revoke" in url:
            self.revoke_calls += 1
            self.revoked = True
            return httpx.Response(200)
        if url.startswith("https://upload.fake/session/"):
            return self._session(r, url)
        if not self._authorized(r):
            return gerr(401, "authError", "Invalid Credentials")
        if self.api_401_once and "youtube/v3/videos" in url and r.method == "GET":
            self.api_401_once = False
            return gerr(401, "authError", "Invalid Credentials")
        if "youtubeanalytics.googleapis.com/v2/reports" in url:
            return self._analytics(r)
        if "upload/youtube/v3/videos" in url and r.method == "POST":
            return self._init(r)
        if "upload/youtube/v3/thumbnails/set" in url:
            return self._thumbnail(r)
        if "youtube/v3/channels" in url:
            return httpx.Response(200, json={"items": self.channels})
        if "youtube/v3/videos" in url:
            return self._videos(r)
        return httpx.Response(404, json={"error": f"unmocked {r.method} {url}"})

    def _token(self, r: httpx.Request) -> httpx.Response:
        form = parse_qs(r.content.decode())
        if form.get("grant_type") == ["refresh_token"]:
            if self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant",
                                                 "error_description": "Token has been expired or revoked."})
            tok = f"at-{len(self.valid_tokens) + 1}"
            self.valid_tokens.add(tok)
            return httpx.Response(200, json={"access_token": tok, "expires_in": 3599, "scope": self.scope,
                                             "token_type": "Bearer"})
        body: dict[str, Any] = {"access_token": "at-1", "expires_in": 3599, "scope": self.scope, "token_type": "Bearer"}
        if self.return_refresh_token:
            body["refresh_token"] = "rt-1"
        return httpx.Response(200, json=body)

    def _init(self, r: httpx.Request) -> httpx.Response:
        if self.init_error is not None:
            return self.init_error
        body = json.loads(r.content)
        self.init_bodies.append(body)
        uri = f"https://upload.fake/session/{len(self.sessions) + 1}"
        self.sessions[uri] = {"size": int(r.headers["X-Upload-Content-Length"]), "received": 0, "body": body}
        return httpx.Response(200, headers={"Location": uri})

    def _session(self, r: httpx.Request, url: str) -> httpx.Response:
        sess = self.sessions.get(url.split("?")[0])
        cr = r.headers.get("Content-Range", "")
        if sess is None:
            return httpx.Response(404)
        if cr.startswith("bytes */"):                       # status query
            if self.expire_sessions:
                return httpx.Response(404)
            if sess.get("video_id"):
                return httpx.Response(200, json=self.videos[sess["video_id"]])
            return self._incomplete(sess)
        self._data_puts += 1
        self.put_ranges.append(cr)
        if self._data_puts in self.drop_puts:
            raise httpx.ConnectError("connection reset by peer", request=r)
        if self.fail_puts:
            return httpx.Response(self.fail_puts.pop(0), text="Service Unavailable")
        m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
        assert m, cr
        start, end = int(m.group(1)), int(m.group(2))
        assert start == sess["received"], f"chunk starts at {start}, session has {sess['received']}"
        assert len(r.content) == end - start + 1
        sess["received"] = end + 1
        if sess["received"] < sess["size"]:
            return self._incomplete(sess)
        vid = f"vid{next(self._ids)}"
        status = dict(sess["body"]["status"], uploadStatus="uploaded")
        if self.privacy_override:
            status["privacyStatus"] = self.privacy_override
        self.videos[vid] = {"id": vid, "snippet": dict(sess["body"]["snippet"]), "status": status}
        sess["video_id"] = vid
        if self.lose_final_response:
            self.lose_final_response = False
            raise httpx.ReadError("connection closed before the response", request=r)
        return httpx.Response(201, json=self.videos[vid])

    @staticmethod
    def _incomplete(sess: dict[str, Any]) -> httpx.Response:
        headers = {"Range": f"bytes=0-{sess['received'] - 1}"} if sess["received"] else {}
        return httpx.Response(308, headers=headers)

    def _analytics(self, r: httpx.Request) -> httpx.Response:
        q = parse_qs(urlparse(str(r.url)).query)
        self.analytics_requests.append(q)
        if "https://www.googleapis.com/auth/yt-analytics.readonly" not in self.scope:
            return gerr(403, "insufficientPermissions", "Request had insufficient authentication scopes.")
        metrics = q["metrics"][0].split(",")
        ids = q["filters"][0].removeprefix("video==").split(",")
        # columns in the order the report declares, dimension first (as the API does)
        headers = [{"name": "video", "columnType": "DIMENSION", "dataType": "STRING"}] + [
            {"name": m, "columnType": "METRIC", "dataType": "INTEGER"} for m in metrics]
        rows = [[vid] + [self.analytics[vid].get(m, 0) for m in metrics] for vid in ids if vid in self.analytics]
        return httpx.Response(200, json={"kind": "youtubeAnalytics#resultTable", "columnHeaders": headers,
                                         "rows": rows})

    def _thumbnail(self, r: httpx.Request) -> httpx.Response:
        if self.thumbnail_error is not None:
            return self.thumbnail_error
        vid = parse_qs(urlparse(str(r.url)).query)["videoId"][0]
        self.thumbnails[vid] = {"type": r.headers["Content-Type"], "bytes": len(r.content)}
        return httpx.Response(200, json={"items": [{"default": {"url": f"https://i.ytimg.com/vi/{vid}/default.jpg"}}]})

    def _videos(self, r: httpx.Request) -> httpx.Response:
        q = parse_qs(urlparse(str(r.url)).query)
        if r.method == "GET":
            v = self.videos.get(q["id"][0])
            items = [dict(v, statistics={"viewCount": "120", "likeCount": "9", "commentCount": "2"})] if v else []
            return httpx.Response(200, json={"items": items})
        if r.method == "PUT":
            body = json.loads(r.content)
            if body["id"] not in self.videos:
                return gerr(404, "videoNotFound")
            self.videos[body["id"]].update(snippet=body["snippet"], status=body["status"])
            return httpx.Response(200, json=self.videos[body["id"]])
        if r.method == "DELETE":
            if self.videos.pop(q["id"][0], None) is None:
                return gerr(404, "videoNotFound")
            return httpx.Response(204)
        return httpx.Response(405)
