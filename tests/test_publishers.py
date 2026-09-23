"""Publishing adapters exercised end to end against mocked platform APIs
(httpx.MockTransport). The mocks follow the documented request/response shapes
of each official API; live posting needs real OAuth apps (REMAINING_EXTERNAL_SETUP.md)."""

import json
import re
import time
from datetime import UTC
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ezra import secrets
from ezra.jobs import RetryableError
from ezra.publishing import providers
from ezra.publishing.base import PostRequest, PublishError, pkce_pair


@pytest.fixture
def video(tmp_path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * (7 * 1024 * 1024 + 123))   # > TikTok's 5 MB chunk floor
    return p


def req(video: Path, **kw) -> PostRequest:
    base = dict(video=video, title="How I lost $400k", caption="I lost $400k overnight #founderstories",
                description="desc", hashtags=["#founderstories"])
    base.update(kw)
    return PostRequest(**base)


class Recorder:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for (method, pattern), fn in self.routes.items():
            if request.method == method and re.search(pattern, str(request.url)):
                return fn(request)
        return httpx.Response(404, json={"error": f"unmocked {request.method} {request.url}"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def test_pkce_pair_is_s256():
    import base64
    import hashlib

    v, c = pkce_pair()
    assert base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode() == c


def test_youtube_oauth_upload_schedule_and_metrics(video):
    secrets.put("youtube-client", {"client_id": "cid", "client_secret": "csec"})
    uploaded = {}

    def init(r):
        body = json.loads(r.content)
        uploaded["meta"] = body
        assert r.headers["X-Upload-Content-Length"] == str(video.stat().st_size)
        return httpx.Response(200, headers={"Location": "https://upload.example/session/1"})

    def put(r):
        uploaded["bytes"] = len(r.content)
        return httpx.Response(200, json={"id": "yt123", "status": {"privacyStatus": "private"}})

    rec = Recorder({
        ("POST", r"oauth2\.googleapis\.com/token"): lambda r: httpx.Response(200, json={
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
        ("GET", r"youtube/v3/channels"): lambda r: httpx.Response(200, json={
            "items": [{"id": "UC1", "snippet": {"title": "My Channel"}}]}),
        ("POST", r"upload/youtube/v3/videos"): init,
        ("PUT", r"upload\.example/session/1"): put,
        ("GET", r"youtube/v3/videos\?"): lambda r: httpx.Response(200, json={
            "items": [{"statistics": {"viewCount": "4210", "likeCount": "300", "commentCount": "12"}}]}),
    })
    yt = providers.YouTubePublisher(rec.client())
    url = yt.authorize_url("st", "http://localhost:8000/cb", "chal")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid"] and q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    tok = yt.exchange_code("code", "http://localhost:8000/cb", "verifier")
    assert tok["account"] == "My Channel" and tok["expires_at"] > time.time()
    secrets.put("youtube:My Channel", tok)
    acc = {"handle": "My Channel", "credential_ref": "youtube:My Channel", "meta": {}}
    from datetime import datetime, timedelta

    when = datetime.now(UTC) + timedelta(days=1)
    res = yt.publish(acc, req(video, scheduled_at=when), "youtube")
    assert res.status == "scheduled" and res.url == "https://www.youtube.com/shorts/yt123"
    assert uploaded["meta"]["status"]["privacyStatus"] == "private" and "publishAt" in uploaded["meta"]["status"]
    assert uploaded["bytes"] == video.stat().st_size
    m = yt.metrics(acc, "yt123")
    assert (m.views, m.likes, m.comments) == (4210, 300, 12)


def test_youtube_refreshes_expired_token(video):
    secrets.put("youtube-client", {"client_id": "cid", "client_secret": "csec"})
    secrets.put("youtube:me", {"access_token": "old", "refresh_token": "rt", "expires_at": time.time() - 10})
    seen = {}
    rec = Recorder({
        ("POST", r"oauth2\.googleapis\.com/token"): lambda r: httpx.Response(200, json={
            "access_token": "new", "expires_in": 3600}),
        ("GET", r"youtube/v3/videos"): lambda r: (seen.setdefault("auth", r.headers["Authorization"]),
                                                  httpx.Response(200, json={"items": []}))[1],
    })
    yt = providers.YouTubePublisher(rec.client())
    assert yt.metrics({"credential_ref": "youtube:me"}, "x") is None
    assert seen["auth"] == "Bearer new" and secrets.get("youtube:me")["access_token"] == "new"


def test_tiktok_chunked_upload_status_and_metrics(video):
    secrets.put("tiktok:me", {"access_token": "at", "expires_at": time.time() + 3600})
    ranges = []

    def init(r):
        body = json.loads(r.content)
        assert body["source_info"]["source"] == "FILE_UPLOAD"
        assert body["post_info"]["privacy_level"] == "SELF_ONLY"      # unaudited client
        assert body["source_info"]["total_chunk_count"] == 1          # 7 MB: a single chunk...
        assert body["source_info"]["chunk_size"] == body["source_info"]["video_size"]   # ...of exactly its size
        return httpx.Response(200, json={"data": {"publish_id": "p1", "upload_url": "https://up.tiktok/1"},
                                         "error": {"code": "ok"}})

    def chunk(r):
        ranges.append(r.headers["Content-Range"])
        return httpx.Response(201)

    rec = Recorder({
        ("POST", r"post/publish/video/init"): init,
        ("PUT", r"up\.tiktok/1"): chunk,
        ("POST", r"post/publish/status/fetch"): lambda r: httpx.Response(200, json={
            "data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300000000001]}}),
        ("POST", r"video/query"): lambda r: httpx.Response(200, json={"data": {"videos": [
            {"id": "7300000000001", "view_count": 9000, "like_count": 700, "comment_count": 40,
             "share_count": 15}]}}),
    })
    tt = providers.TikTokPublisher(rec.client())
    acc = {"handle": "me", "credential_ref": "tiktok:me", "meta": {"unaudited": True}}
    res = tt.publish(acc, req(video), "tiktok")
    size = video.stat().st_size
    assert ranges == [f"bytes 0-{size - 1}/{size}"]
    assert res.status == "published" and res.external_id == "7300000000001"
    m = tt.metrics(acc, res.external_id)
    assert (m.views, m.shares) == (9000, 15)


def test_tiktok_failed_publish_raises(video):
    secrets.put("tiktok:me", {"access_token": "at"})
    rec = Recorder({
        ("POST", r"init"): lambda r: httpx.Response(200, json={"data": {"publish_id": "p", "upload_url": "https://u/1"},
                                                               "error": {"code": "ok"}}),
        ("PUT", r"https://u/1"): lambda r: httpx.Response(201),
        ("POST", r"status/fetch"): lambda r: httpx.Response(200, json={"data": {"status": "FAILED",
                                                                                "fail_reason": "spam_risk"}}),
    })
    with pytest.raises(PublishError, match="spam_risk"):
        providers.TikTokPublisher(rec.client()).publish({"credential_ref": "tiktok:me", "meta": {}}, req(video),
                                                        "tiktok")


def test_instagram_resumable_reel_publish_and_insights(video):
    secrets.put("instagram:me", {"access_token": "at", "ig_user_id": "1784", "expires_at": time.time() + 99999})
    polls = iter(["IN_PROGRESS", "FINISHED"])
    got = {}

    def upload(r):
        got["auth"], got["size"] = r.headers["Authorization"], r.headers["file_size"]
        return httpx.Response(200, json={"success": True})

    rec = Recorder({
        ("POST", r"graph\.instagram\.com/v25\.0/1784/media$"): lambda r: httpx.Response(200, json={"id": "c1"}),
        ("POST", r"rupload\.facebook\.com/ig-api-upload/v25\.0/c1"): upload,
        ("GET", r"/c1\?"): lambda r: httpx.Response(200, json={"status_code": next(polls)}),
        ("POST", r"1784/media_publish"): lambda r: httpx.Response(200, json={"id": "m9"}),
        ("GET", r"/m9\?fields=permalink"): lambda r: httpx.Response(200, json={"permalink": "https://ig/p/m9"}),
        ("GET", r"/m9/insights"): lambda r: httpx.Response(200, json={"data": [
            {"name": "views", "values": [{"value": 5000}]}, {"name": "saved", "values": [{"value": 44}]}]}),
    })
    ig = providers.InstagramPublisher(rec.client())
    ig.poll_seconds = 0
    acc = {"credential_ref": "instagram:me", "meta": {}}
    res = ig.publish(acc, req(video), "instagram")
    assert res.url == "https://ig/p/m9" and got == {"auth": "OAuth at", "size": str(video.stat().st_size)}
    m = ig.metrics(acc, "m9")
    assert (m.views, m.saves) == (5000, 44)
    with pytest.raises(PublishError, match="no private posts"):
        ig.publish(acc, req(video, visibility="private"), "instagram")


def test_rate_limits_are_retryable(video):
    secrets.put("tiktok:me", {"access_token": "at"})
    rec = Recorder({("POST", r"init"): lambda r: httpx.Response(429, text="slow down")})
    with pytest.raises(RetryableError):
        providers.TikTokPublisher(rec.client()).publish({"credential_ref": "tiktok:me", "meta": {}}, req(video),
                                                        "tiktok")


def test_missing_credentials_are_explicit(video):
    yt = providers.YouTubePublisher(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(PublishError, match="no stored credential"):
        yt.publish({"handle": "x", "credential_ref": None}, req(video), "youtube")
    with pytest.raises(secrets.SecretError, match="YOUTUBE_CLIENT_SECRET_JSON"):
        yt.authorize_url("s", "r", "c")


def test_local_export_writes_a_post_folder(video, tmp_path):
    res = providers.LocalExportPublisher().publish({"meta": {"dir": str(tmp_path / "out")}}, req(video), "tiktok")
    folder = Path(res.raw["dir"])
    assert (folder / "video.mp4").exists() and json.loads((folder / "post.json").read_text())["platform"] == "tiktok"


def test_tiktok_multi_chunk_ranges(tmp_path):
    big = tmp_path / "big.mp4"
    big.write_bytes(b"\x00" * (25 * 1024 * 1024 + 7))       # 2 chunks of 10 MB, the last absorbs 5 MB + 7 B
    secrets.put("tiktok:me", {"access_token": "at"})
    ranges, init_body = [], {}

    def init(r):
        init_body.update(json.loads(r.content)["source_info"])
        return httpx.Response(200, json={"data": {"publish_id": "p", "upload_url": "https://u/1"},
                                         "error": {"code": "ok"}})

    def chunk(r):
        ranges.append(r.headers["Content-Range"])
        return httpx.Response(201)

    rec = Recorder({("POST", r"init"): init, ("PUT", r"https://u/1"): chunk,
                    ("POST", r"status/fetch"): lambda r: httpx.Response(
                        200, json={"data": {"status": "PROCESSING_UPLOAD"}})})
    acc = {"credential_ref": "tiktok:me", "meta": {}}
    res = providers.TikTokPublisher(rec.client()).publish(acc, req(big), "tiktok")
    size = big.stat().st_size
    c = 10 * 1024 * 1024
    assert init_body["total_chunk_count"] == 2 and init_body["chunk_size"] == c
    assert ranges == [f"bytes 0-{c - 1}/{size}", f"bytes {c}-{size - 1}/{size}"]
    assert res.status == "processing"
