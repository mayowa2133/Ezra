"""YouTube publisher against a stateful fake of Google's API (tests/youtube_fake.py): OAuth,
chunked resumable uploads that resume instead of duplicating, error classification,
private-only mode, scheduling, thumbnails, metadata limits, lifecycle and edit/delete."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from PIL import Image

from ezra import secrets
from ezra.config import reset_settings
from ezra.publishing.base import PostRequest, PublishError, RetryablePublishError
from ezra.publishing.youtube import ANALYTICS, MANAGE, YouTubePublisher, clean_tags, clean_title, prepare_thumbnail
from tests.youtube_fake import FakeYouTube, gerr

MB = 1024 * 1024


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("EZRA_YOUTUBE_CHUNK_MB", "1")
    reset_settings()
    secrets.put("youtube-client", {"installed": {"client_id": "cid", "client_secret": "csec"}})
    secrets.put("youtube:Ezra Test", {"access_token": "at-1", "refresh_token": "rt-1",
                                      "expires_at": time.time() + 3000})
    return FakeYouTube()


@pytest.fixture
def video(tmp_path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(bytes(range(256)) * (4 * MB // 256) + b"tail")      # 4 MiB + 4 bytes: 5 chunks of 1 MiB
    return p


ACC = {"handle": "Ezra Test", "credential_ref": "youtube:Ezra Test", "meta": {"channel_id": "UC123"}}


def yt(fake: FakeYouTube) -> YouTubePublisher:
    p = YouTubePublisher(fake.client())
    p.sleep = lambda s: None
    return p


def req(video: Path, **kw) -> PostRequest:
    base = dict(video=video, title="Jungle vs Desert", caption="3 teams #mrbeast", description="desc",
                hashtags=["#mrbeast", "#challenge"], visibility="private")
    base.update(kw)
    return PostRequest(**base)


def allow_public(monkeypatch):
    monkeypatch.setenv("EZRA_YOUTUBE_PUBLIC_ALLOWED", "1")
    reset_settings()


# --- OAuth ---------------------------------------------------------------------------------------

def test_authorize_url_requests_offline_pkce_and_analytics(fake, monkeypatch):
    q = parse_qs(urlparse(yt(fake).authorize_url("st", "http://127.0.0.1:8765/callback", "chal")).query)
    scopes = q["scope"][0].split()
    assert ANALYTICS in scopes and MANAGE not in scopes
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"] and q["code_challenge_method"] == ["S256"]
    monkeypatch.setenv("EZRA_YOUTUBE_MANAGE", "1")
    reset_settings()
    assert MANAGE in parse_qs(urlparse(yt(fake).authorize_url("s", "r", "c")).query)["scope"][0]


def test_exchange_stores_channel_scopes_and_refresh_token(fake):
    tok = yt(fake).exchange_code("code", "http://127.0.0.1:8765/callback", "verifier")
    assert (tok["account"], tok["channel_id"], tok["refresh_token"]) == ("Ezra Test", "UC123", "rt-1")
    assert ANALYTICS in tok["scopes"] and tok["expires_at"] > time.time()


def test_exchange_without_refresh_token_or_channel_is_explained(fake):
    fake.return_refresh_token = False
    with pytest.raises(PublishError) as e:
        yt(fake).exchange_code("code", "r", "v")
    assert e.value.code == "refresh_token_missing" and e.value.reconnect
    fake.return_refresh_token, fake.channels = True, []
    with pytest.raises(PublishError, match="no YouTube channel"):
        yt(fake).exchange_code("code", "r", "v")


def test_expired_token_refreshes_and_revoked_token_asks_for_reconnect(fake, video):
    secrets.put("youtube:Ezra Test", {"access_token": "old", "refresh_token": "rt-1", "expires_at": time.time() - 5})
    assert yt(fake).metrics(ACC, "none") is None
    stored = secrets.get("youtube:Ezra Test")
    assert stored["access_token"] != "old" and stored["refresh_token"] == "rt-1"   # refresh token kept
    fake.revoked = True
    secrets.put("youtube:Ezra Test", {**stored, "expires_at": time.time() - 5})
    with pytest.raises(PublishError) as e:
        yt(fake).publish(ACC, req(video), "youtube")
    assert e.value.code == "auth_revoked" and e.value.reconnect and e.value.permanent


def test_a_401_forces_one_refresh(fake, video):
    pub = yt(fake)
    res = pub.publish(ACC, req(video, thumbnail=None), "youtube")
    fake.api_401_once = True
    assert pub.refresh_status(ACC, res.external_id).platform_state == "processing"
    assert len(fake.valid_tokens) == 2                      # one new access token


# --- uploads ---------------------------------------------------------------------------------------

def test_private_upload_is_chunked_with_progress_and_state(fake, video):
    states, progress = [], []
    res = yt(fake).publish(ACC, req(video, on_state=states.append,
                                    on_progress=lambda a, b: progress.append((a, b))), "youtube")
    size = video.stat().st_size
    assert res.status == "published" and res.platform_state == "processing" and res.external_id == "vid1"
    assert res.url == "https://www.youtube.com/shorts/vid1"
    assert fake.put_ranges == [f"bytes {i * MB}-{min(size, (i + 1) * MB) - 1}/{size}" for i in range(5)]
    assert [a for a, _ in progress] == sorted(a for a, _ in progress) and progress[-1] == (size, size)
    assert states[0]["session_uri"] and states[-1]["video_id"] == "vid1"
    body = fake.init_bodies[0]
    assert body["status"]["privacyStatus"] == "private" and "publishAt" not in body["status"]
    assert body["snippet"]["tags"] == ["mrbeast", "challenge"] and body["snippet"]["categoryId"] == "22"


def test_a_lost_final_response_recovers_the_video_instead_of_uploading_again(fake, video):
    fake.lose_final_response = True
    states: list[dict] = []
    pub = yt(fake)
    pub.max_chunk_attempts = 1
    with pytest.raises(RetryablePublishError):
        pub.publish(ACC, req(video, on_state=states.append), "youtube")
    assert len(fake.videos) == 1                            # YouTube has it; we never heard back
    res = pub.publish(ACC, req(video, upload_state=states[-1]), "youtube")   # the job's retry
    assert res.external_id == "vid1" and len(fake.videos) == 1 and len(fake.sessions) == 1


def test_a_dropped_connection_mid_upload_resumes_from_the_last_byte(fake, video):
    fake.drop_puts = [3]
    res = yt(fake).publish(ACC, req(video), "youtube")
    assert res.external_id == "vid1" and len(fake.sessions) == 1
    assert fake.put_ranges[3] == fake.put_ranges[2]         # chunk 3 resent after a status query


def test_exhausted_chunk_retries_leave_a_resumable_session(fake, video):
    states: list[dict] = []
    pub = yt(fake)
    pub.max_chunk_attempts = 2
    fake.drop_puts = [3, 4]                                  # chunk 3 fails twice in a row
    with pytest.raises(RetryablePublishError):
        pub.publish(ACC, req(video, on_state=states.append), "youtube")
    assert states[-1]["bytes_sent"] == 2 * MB
    before = len(fake.put_ranges)
    res = pub.publish(ACC, req(video, upload_state=states[-1]), "youtube")
    assert res.external_id == "vid1" and len(fake.sessions) == 1
    assert fake.put_ranges[before].startswith(f"bytes {2 * MB}-")   # resumed, not restarted


def test_5xx_on_a_chunk_is_retried(fake, video):
    fake.fail_puts = [503, 502]
    assert yt(fake).publish(ACC, req(video), "youtube").external_id == "vid1"


def test_an_expired_session_starts_a_new_one(fake, video):
    pub = yt(fake)
    states: list[dict] = []
    fake.drop_puts = [2]
    pub.max_chunk_attempts = 1
    with pytest.raises(RetryablePublishError):
        pub.publish(ACC, req(video, on_state=states.append), "youtube")
    fake.expire_sessions = True
    res = pub.publish(ACC, req(video, upload_state=states[-1]), "youtube")
    assert res.external_id == "vid1" and len(fake.sessions) == 2 and len(fake.videos) == 1


@pytest.mark.parametrize("response,code,permanent", [
    (gerr(403, "quotaExceeded"), "quota_exceeded", True),
    (gerr(403, "rateLimitExceeded"), "rate_limited", False),
    (gerr(403, "userRateLimitExceeded"), "rate_limited", False),
    (gerr(400, "uploadLimitExceeded"), "upload_limit", True),
    (gerr(400, "invalidTitle"), "invalid_metadata", True),
    (gerr(403, "insufficientPermissions"), "scope_missing", True),
    (httpx.Response(429, text="slow"), "rate_limited", False),
    (httpx.Response(503, text="down"), "server_error", False),
])
def test_google_errors_are_classified(fake, video, response, code, permanent):
    fake.init_error = response
    with pytest.raises((PublishError, RetryablePublishError)) as e:
        yt(fake).publish(ACC, req(video), "youtube")
    assert e.value.code == code
    assert getattr(e.value, "permanent", False) == permanent
    if code == "quota_exceeded":
        assert "midnight Pacific" in str(e.value)


# --- visibility and scheduling ------------------------------------------------------------------

def test_public_and_scheduled_uploads_are_refused_until_the_project_is_audited(fake, video):
    for kw in ({"visibility": "public"}, {"scheduled_at": datetime.now(UTC) + timedelta(days=1)}):
        with pytest.raises(PublishError) as e:
            yt(fake).publish(ACC, req(video, **kw), "youtube")
        assert e.value.code == "private_only"
    assert not fake.sessions


def test_scheduling_converts_the_timezone_and_rejects_the_past(fake, video, monkeypatch):
    allow_public(monkeypatch)
    when = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=2)).replace(hour=16, minute=0, second=0,
                                                                                    microsecond=0)
    res = yt(fake).publish(ACC, req(video, visibility="public", scheduled_at=when), "youtube")
    st = fake.init_bodies[0]["status"]
    assert res.status == "scheduled" and res.platform_state == "scheduled"
    assert st["privacyStatus"] == "private"
    assert st["publishAt"] == when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with pytest.raises(PublishError) as e:
        yt(fake).publish(ACC, req(video, visibility="public", scheduled_at=datetime.now(UTC) - timedelta(hours=1)),
                         "youtube")
    assert e.value.code == "invalid_schedule"


def test_a_public_upload_locked_private_is_flagged(fake, video, monkeypatch):
    allow_public(monkeypatch)
    fake.privacy_override = "private"                        # what an unaudited project gets
    res = yt(fake).publish(ACC, req(video, visibility="public"), "youtube")
    assert [w["code"] for w in res.warnings] == ["locked_private"]


# --- thumbnails -----------------------------------------------------------------------------------

def _image(path: Path, size=(1080, 1920), fmt="JPEG", noise=False) -> Path:
    import numpy as np

    arr = (np.random.default_rng(0).integers(0, 255, (size[1], size[0], 3), dtype=np.uint8) if noise
           else np.full((size[1], size[0], 3), 120, dtype=np.uint8))
    Image.fromarray(arr).save(path, fmt)
    return path


def test_thumbnail_is_set_after_the_upload(fake, video, tmp_path):
    res = yt(fake).publish(ACC, req(video, thumbnail=_image(tmp_path / "t.jpg")), "youtube")
    assert fake.thumbnails["vid1"]["type"] == "image/jpeg" and not res.warnings


def test_an_oversized_png_is_reencoded_under_2mb(tmp_path):
    data, mime = prepare_thumbnail(_image(tmp_path / "big.png", (2400, 4200), "PNG", noise=True))
    assert mime == "image/jpeg" and len(data) <= 2 * MB
    junk = tmp_path / "x.jpg"
    junk.write_bytes(b"nope")
    with pytest.raises(ValueError, match="not a readable image"):
        prepare_thumbnail(junk)


def test_a_failed_thumbnail_is_a_warning_not_a_failed_upload(fake, video, tmp_path):
    fake.thumbnail_error = gerr(403, "forbidden", "The authenticated user doesn't have permissions")
    res = yt(fake).publish(ACC, req(video, thumbnail=_image(tmp_path / "t.jpg")), "youtube")
    assert res.external_id == "vid1"
    assert res.warnings[0]["code"] == "thumbnail_failed" and "verify" in res.warnings[0]["message"]
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    res2 = yt(fake).publish(ACC, req(video, thumbnail=bad), "youtube")
    assert res2.warnings[0]["code"] == "thumbnail_invalid"


# --- metadata limits -------------------------------------------------------------------------------

def test_metadata_is_cleaned_to_youtube_limits():
    long = "A <very> long title " * 10
    t = clean_title(long)
    assert len(t) <= 100 and "<" not in t and not t.endswith(" ")
    tags = clean_tags([f"#tag number {i}" for i in range(80)] + ["#tag number 1", "a<b", "x,y"])
    total = sum(len(x) + (2 if " " in x else 0) for x in tags) + len(tags) - 1
    assert total <= 500 and len(set(t.lower() for t in tags)) == len(tags) and "tag number 0" in tags


# --- lifecycle, edit, delete -------------------------------------------------------------------------

def test_status_edit_reschedule_and_delete(fake, video, monkeypatch):
    allow_public(monkeypatch)
    pub = yt(fake)
    res = pub.publish(ACC, req(video, visibility="public", scheduled_at=datetime.now(UTC) + timedelta(days=1)),
                      "youtube")
    vid = res.external_id
    assert pub.refresh_status(ACC, vid).platform_state == "scheduled"
    with pytest.raises(PublishError) as e:                  # connected without the manage scope
        pub.update_video({**ACC, "meta": {"scopes": [ANALYTICS]}}, vid, title="New")
    assert e.value.code == "scope_missing"
    manage = {**ACC, "meta": {"scopes": [MANAGE]}}
    later = datetime.now(UTC) + timedelta(days=3)
    pub.update_video(manage, vid, title="New <title>", publish_at=later)
    assert fake.videos[vid]["snippet"]["title"] == "New title"
    assert fake.videos[vid]["status"]["publishAt"] == later.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    pub.update_video(manage, vid, publish_at=None)          # cancel the schedule: stays private
    assert "publishAt" not in fake.videos[vid]["status"]
    fake.videos[vid]["status"].update(uploadStatus="processed", privacyStatus="public")
    assert pub.refresh_status(ACC, vid).platform_state == "public"
    pub.delete_video(manage, vid)
    assert pub.refresh_status(ACC, vid).platform_state == "deleted"


def test_revoke_calls_google(fake):
    assert yt(fake).revoke({"refresh_token": "rt-1"}) and fake.revoke_calls == 1
