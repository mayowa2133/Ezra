"""Buffer contracts and the approval → submit → verify service path, without live posts."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ezra import db, jobs, publishing, review, secrets
from ezra.config import reset_settings
from ezra.db.models import Candidate, Clip, ClipVersion, Post, Source
from ezra.publishing import scheduler
from ezra.publishing.base import PostRequest, PublishError, RetryablePublishError
from ezra.publishing.buffer import BufferPublisher, public_media_url
from ezra.storage import get_storage


class FakeBuffer:
    def __init__(self):
        self.calls = []
        self.posts = {}
        self.create_status = 200
        self.create_error = None
        self.lose_response = False
        self.status = "scheduled"
        self.channel = {
            "id": "c1",
            "service": "youtube",
            "name": "Ezra Test",
            "displayName": "Ezra Test",
            "isDisconnected": False,
            "isLocked": False,
            "isQueuePaused": False,
        }

    def __call__(self, request):
        self.calls.append(request)
        if request.url.host == "api.cloudinary.com":
            assert request.headers["authorization"].startswith("Basic ")
            assert b"ezra-buffer/" in request.content
            return httpx.Response(200, json={"secure_url": "https://res.cloudinary.com/test/video/upload/clip.mp4"})
        assert str(request.url) == "https://api.buffer.com"
        assert request.headers["authorization"] == "Bearer buffer-test-key"
        body = json.loads(request.content)
        query, payload = body["query"], body["variables"].get("input", {})
        if "EzraChannels" in query:
            data = {"channels": [self.channel]}
        elif "organizations" in query:
            data = {"account": {"organizations": [{"id": "org", "name": "Test"}]}}
        elif "EzraPublish" in query:
            if self.create_status != 200:
                return httpx.Response(self.create_status, json={"message": "do not expose raw errors"})
            if self.create_error:
                return httpx.Response(
                    200, json={"data": {"createPost": {"__typename": self.create_error, "message": "refused"}}}
                )
            self.posts["p1"] = payload
            if self.lose_response:
                raise httpx.ReadTimeout("lost response", request=request)
            data = {"createPost": {"__typename": "PostActionSuccess", "post": self.post()}}
        elif "EzraCancel" in query:
            self.posts.pop(payload["id"], None)
            data = {"deletePost": {"__typename": "DeletePostSuccess"}}
        elif "EzraReschedule" in query:
            self.posts[payload["id"]].update(payload)
            data = {"editPost": {"post": {"id": "p1"}}}
        elif "EzraMetrics" in query:
            data = {"post": {"metrics": [{"type": "views", "value": 123}, {"type": "reactions", "value": 4}]}}
        elif "EzraPost" in query:
            data = {"post": self.post()}
        else:
            raise AssertionError(query)
        return httpx.Response(200, json={"data": data})

    def post(self):
        return {
            "id": "p1",
            "status": self.status,
            "externalLink": "https://youtube.com/shorts/live" if self.status == "sent" else None,
            "schedulingType": "automatic",
            "error": None,
        }

    def creates(self):
        return [r for r in self.calls if b"EzraPublish" in r.content]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "buffer-test-key")
    monkeypatch.setenv("EZRA_CLOUDINARY_JSON", json.dumps({"cloud_name": "test", "api_key": "ck", "api_secret": "cs"}))
    fake = FakeBuffer()
    publishing.set_http_client(httpx.Client(transport=httpx.MockTransport(fake)))
    yield fake
    publishing.set_http_client(None)


def clip():
    with db.session() as s:
        source = Source(title="Test", storage_key="source.mp4", sha256="a" * 64, rights_status="authorized")
        s.add(source)
        s.flush()
        candidate = Candidate(source_id=source.id, start=0, end=20, compliance_status="PASS")
        s.add(candidate)
        s.flush()
        row = Clip(
            source_id=source.id,
            candidate_id=candidate.id,
            status="rendered",
            title="Test",
            platform_metadata={
                "youtube": {
                    "title": "Test",
                    "caption": "Caption #required",
                    "description": "Description",
                    "hashtags": ["required"],
                }
            },
        )
        s.add(row)
        s.flush()
        version = ClipVersion(clip_id=row.id, version=1, status="rendered", video_key="clip.mp4")
        s.add(version)
        s.flush()
        row.current_version_id = version.id
        cid = row.id
    get_storage().put_bytes("clip.mp4", b"test-video")
    return cid


def test_discover_import_and_health_without_upload(fake):
    found = publishing.buffer_channels()
    assert found[0]["channel_id"] == "c1"
    accounts = publishing.connect_buffer(tz="America/Toronto")
    again = publishing.connect_buffer()
    assert accounts[0]["id"] == again[0]["id"]
    health = publishing.account_health(accounts[0]["id"], check_live=True)
    assert health["has_token"] and health["media_configured"] and health["live_check"]["ok"]
    assert "buffer-test-key" not in str(accounts) + str(health)
    assert not fake.creates() and all(r.url.host == "api.buffer.com" for r in fake.calls)
    with pytest.raises(ValueError, match="not found"):
        publishing.connect_buffer(["missing"])


def test_approval_dry_run_scheduled_delivery_metrics_and_duplicates(fake):
    publishing.connect_buffer()
    cid = clip()
    assert publishing.publish_clip(cid, ["youtube"], confirm=True)["problems"]
    assert not fake.creates()
    review.approve(cid)
    dry = publishing.publish_clip(cid, ["youtube"])
    assert dry["dry_run"] and not dry["problems"] and not fake.creates()
    when = datetime.now(UTC) + timedelta(days=1)
    result = publishing.publish_clip(cid, ["youtube"], schedule_at=when, confirm=True)
    pid = result["post_ids"][0]
    publishing.run_publish(pid)
    post = publishing.get_post(pid)
    assert post.status == "scheduled" and post.external_id == "p1" and post.url is None
    with db.session() as s:
        assert s.get(Clip, cid).status == "approved"
    assert fake.posts["p1"]["mode"] == "customScheduled"
    assert fake.posts["p1"]["metadata"]["youtube"]["title"] == "Test"
    assert "#required" in fake.posts["p1"]["text"]
    assert fake.posts["p1"]["schedulingType"] == "automatic"
    publishing.run_publish(pid)
    assert len(fake.creates()) == 1
    assert publishing.publish_clip(cid, ["youtube"], confirm=True)["problems"]
    with db.session() as s:
        s.get(Post, pid).scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
    assert pid not in scheduler.due_posts()
    scheduler.tick()
    assert any(j.kind == "refresh_post_status" for j in jobs.list_jobs())
    fake.status = "sent"
    assert publishing.refresh_statuses()[0]["status"] == "published"
    assert publishing.get_post(pid).url == "https://youtube.com/shorts/live"
    with db.session() as s:
        assert s.get(Clip, cid).status == "published"
    metrics = BufferPublisher(publishing._http_client).metrics({}, "p1")
    assert metrics.views == 123 and metrics.likes == 4 and metrics.shares is None


def test_cancel_and_reschedule_update_buffer_first(fake):
    publishing.connect_buffer()
    cid = clip()
    review.approve(cid)
    result = publishing.publish_clip(cid, ["youtube"], schedule_at=datetime.now(UTC) + timedelta(days=1), confirm=True)
    pid = result["post_ids"][0]
    publishing.run_publish(pid)
    new = datetime.now(UTC) + timedelta(days=2)
    publishing.reschedule_post(pid, new)
    assert fake.posts["p1"]["dueAt"] == new.isoformat()
    publishing.cancel_post(pid)
    assert publishing.get_post(pid).status == "cancelled" and not fake.posts


def test_missing_media_configuration_and_compliance_block_before_queue(fake, monkeypatch):
    publishing.connect_buffer()
    cid = clip()
    review.approve(cid)
    monkeypatch.delenv("EZRA_CLOUDINARY_JSON")
    dry = publishing.publish_clip(cid, ["youtube"], confirm=True)
    assert any("hosting" in p for p in dry["problems"])
    assert not dry.get("job_ids") and not fake.creates()
    with db.session() as s:
        candidate = s.get(Candidate, s.get(Clip, cid).candidate_id)
        candidate.compliance_status = "FAIL"
    assert any("compliance" in p for p in publishing.publish_clip(cid, ["youtube"], confirm=True)["problems"])


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_have_safe_classification(fake, status):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="secret echoed")))
    expected = RetryablePublishError if status in (429, 500) else PublishError
    with pytest.raises(expected) as exc:
        BufferPublisher(client).channels()
    assert "secret echoed" not in str(exc.value)


@pytest.mark.parametrize("mode", ["timeout", "server_error", "malformed"])
def test_uncertain_submission_is_not_repeated(fake, tmp_path, mode):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"test")
    state = {}
    req = PostRequest(video, "Test", "Caption", None, [], on_state=lambda s: state.update(s))
    publisher = BufferPublisher(publishing._http_client)
    account = {"meta": {"channel_id": "c1"}}
    if mode == "timeout":
        fake.lose_response = True
    elif mode == "server_error":
        fake.create_status = 500
    else:
        fake.post = lambda: {"status": "sending"}
    with pytest.raises(PublishError, match=r"unknown|no post ID"):
        publisher.publish(account, req, "youtube")
    assert state["buffer_submission_pending"]
    req.upload_state = state
    with pytest.raises(PublishError, match="possible duplicate"):
        publisher.publish(account, req, "youtube")
    assert len(fake.creates()) == 1


def test_rate_limit_can_retry_and_saved_id_resumes(fake, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"test")
    state = {}
    req = PostRequest(video, "Test", "Caption", None, [], on_state=lambda s: state.update(s))
    publisher = BufferPublisher(publishing._http_client)
    account = {"meta": {"channel_id": "c1"}}
    fake.create_status = 429
    with pytest.raises(RetryablePublishError):
        publisher.publish(account, req, "youtube")
    assert not state["buffer_submission_pending"]
    fake.create_status = 200
    req.upload_state = state.copy()
    publisher.publish(account, req, "youtube")
    assert state["buffer_post_id"] == "p1"
    req.upload_state = state.copy()
    publisher.publish(account, req, "youtube")
    assert len(fake.creates()) == 2  # one rejected by rate limiting, one accepted
    assert sum(r.url.host == "api.cloudinary.com" for r in fake.calls) == 1


@pytest.mark.parametrize("platform", ["youtube", "instagram", "facebook", "tiktok"])
def test_platform_metadata_and_visibility(fake, tmp_path, platform):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"test")
    visibility = "private" if platform == "youtube" else "public"
    req = PostRequest(video, "Title", "Caption", "Description", ["required"], visibility=visibility)
    publisher = BufferPublisher(publishing._http_client)
    fake.status = "sent"
    result = publisher.publish({"meta": {"channel_id": "c1"}}, req, platform)
    payload = fake.posts["p1"]
    assert result.status == "published" and result.platform_state == visibility
    assert "#required" in payload["text"]
    if platform == "youtube":
        assert "Caption" in payload["text"] and "Description" in payload["text"]
        assert payload["metadata"][platform]["privacy"] == "private"
    elif platform in ("instagram", "facebook"):
        assert payload["metadata"][platform]["type"] == "reel"
    else:
        assert payload["metadata"] == {}
    if platform != "youtube":
        with pytest.raises(PublishError, match="only for YouTube"):
            publisher.preflight({"platform": platform}, "private")


@pytest.mark.parametrize("platform", ["instagram", "tiktok", "youtube"])
def test_cover_frame_survives_publish_service(fake, platform):
    fake.channel["service"] = platform
    publishing.connect_buffer()
    cid = clip()
    with db.session() as s:
        row = s.get(Clip, cid)
        row.platform_metadata = {platform: {"caption": "Caption", "hashtags": []}}
        s.get(ClipVersion, row.current_version_id).spec = {"thumbnail_offset_ms": 40800}
    review.approve(cid)
    pid = publishing.publish_clip(cid, [platform], confirm=True)["post_ids"][0]
    publishing.run_publish(pid)
    asset = fake.posts["p1"]["assets"][0]["video"]
    if platform == "youtube":
        assert "metadata" not in asset
    else:
        assert asset["metadata"]["thumbnailOffset"] == 40800
    assert "thumbnailUrl" not in asset


@pytest.mark.parametrize("offset", [-1, True, 1.5, "800"])
def test_invalid_cover_frame_refused_before_upload(fake, tmp_path, offset):
    req = PostRequest(tmp_path / "unused.mp4", "Title", "Caption", None, [], thumbnail_offset_ms=offset)
    with pytest.raises(PublishError, match="non-negative integer"):
        BufferPublisher(publishing._http_client).publish({"meta": {"channel_id": "c1"}}, req, "instagram")
    assert not fake.calls


def test_public_storage_stages_only_render_and_reuses_object(fake, tmp_path, monkeypatch):
    from ezra.publishing import buffer
    from ezra.storage import LocalStorage

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"approved-render")
    staged = LocalStorage(tmp_path / "public")
    monkeypatch.setenv("EZRA_STORAGE", "s3")
    monkeypatch.setenv("EZRA_BUFFER_MEDIA_BASE_URL", "https://media.example.com")
    reset_settings()
    monkeypatch.setattr(buffer, "get_storage", lambda: staged)
    publisher = BufferPublisher(publishing._http_client)
    account = {"platform": "youtube", "meta": {"channel_id": "c1"}}
    publisher.preflight(account, "public")
    assert not list((tmp_path / "public").rglob("*.mp4"))
    req = PostRequest(video, "Title", "Caption", None, [])
    url = publisher._media(req)
    key = url.removeprefix("https://media.example.com/")
    assert key.startswith("buffer-media/") and staged.local_path(key).read_bytes() == b"approved-render"
    monkeypatch.setattr(staged, "put_file", lambda *args: pytest.fail("existing object uploaded twice"))
    assert publisher._media(req) == url
    assert not fake.calls


def test_remote_cancel_failure_preserves_local_schedule(fake):
    publishing.connect_buffer()
    cid = clip()
    review.approve(cid)
    when = datetime.now(UTC) + timedelta(days=1)
    pid = publishing.publish_clip(cid, ["youtube"], schedule_at=when, confirm=True)["post_ids"][0]
    publishing.run_publish(pid)

    def refuse(request):
        return httpx.Response(200, json={"data": {"deletePost": {"__typename": "NotFoundError"}}})

    publishing.set_http_client(httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(PublishError, match="could not cancel"):
        publishing.cancel_post(pid)
    assert publishing.get_post(pid).status == "scheduled" and "p1" in fake.posts


def test_automatic_status_polling_conserves_requests_and_manual_refresh_is_immediate(fake):
    publishing.connect_buffer()
    cid = clip()
    review.approve(cid)
    pid = publishing.publish_clip(cid, ["youtube"], confirm=True)["post_ids"][0]
    publishing.run_publish(pid)
    publishing.refresh_statuses(automatic=True)
    calls = len(fake.calls)
    publishing.refresh_statuses(automatic=True)
    assert len(fake.calls) == calls
    publishing.refresh_statuses()
    assert len(fake.calls) == calls + 1
    with db.session() as s:
        post = s.get(Post, pid)
        post.upload_state = post.upload_state | {
            "buffer_status_checked_at": (datetime.now(UTC) - timedelta(minutes=16)).isoformat()}
    publishing.refresh_statuses(automatic=True)
    assert len(fake.calls) == calls + 2


def test_buffer_retry_after_controls_job_backoff(fake):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
        429, headers={"retry-after": "753"})))
    with pytest.raises(RetryablePublishError) as exc:
        BufferPublisher(client).channels()
    assert exc.value.retry_after == 753
    job = jobs.enqueue("refresh_post_status", {})
    before = datetime.now(UTC)
    jobs._record_failure(job.id, exc.value)
    assert db.aware(jobs.get(job.id).run_after) >= before + timedelta(seconds=753)


@pytest.mark.parametrize("status", ["error", "draft", "needs_approval"])
def test_remote_blocked_states_never_count_as_delivered(fake, status):
    fake.status = status
    with pytest.raises(PublishError):
        BufferPublisher(publishing._http_client).refresh_status({}, "p1")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/v.mp4",
        "https://host/v?X-Amz-Signature=secret",
        "https://user:pass@host/v.mp4",
        "https://localhost/v.mp4",
    ],
)
def test_reject_unusable_media_urls(url):
    with pytest.raises(PublishError):
        public_media_url(url)


def test_api_cli_and_mcp_connection_surfaces(fake):
    from ezra.api.app import app
    from ezra.cli import app as cli
    from ezra.mcp_server import ezra_buffer_channels, ezra_connect_buffer

    with TestClient(app) as client:
        integration = next(p for p in client.get("/api/integrations").json()["publishers"] if p["provider"] == "buffer")
        assert integration["configured"] and integration["env_var"] == "BUFFER_API_KEY"
        saved = client.post("/api/integrations/buffer/credentials", json={"api_key": "stored-buffer-key"})
        assert saved.json() == {"configured": True}
        assert client.post("/api/integrations/buffer/credentials", json={"api_key": "not a key"}).status_code == 400
        assert "stored-buffer-key" not in client.get("/api/integrations").text
        hosted = client.post("/api/integrations/buffer/media-credentials", json={
            "cloud_name": "private-cloud", "api_key": "private-host-key", "api_secret": "private-host-secret"})
        assert hosted.json() == {"configured": True}
        assert "private-host-secret" not in client.get("/api/integrations").text
        assert client.post("/api/integrations/buffer/media-credentials", json={
            "cloud_name": "bad/cloud", "api_key": "private-key", "api_secret": "private-secret"}).status_code == 422
        assert client.get("/api/integrations/buffer/channels").json()[0]["channel_id"] == "c1"
        assert client.post("/api/integrations/buffer/connect", json={"channel_ids": ["c1"]}).status_code == 200
    result = CliRunner().invoke(cli, ["accounts", "buffer-connect", "--channel", "c1"])
    assert result.exit_code == 0, result.output
    assert ezra_buffer_channels()[0]["channel_id"] == "c1"
    assert len(ezra_connect_buffer(["c1"])) == 1
    assert not fake.creates()


def test_secret_aliases_load_dotenv_without_exposing_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("BUFFER_API_KEY=from-file\n")
    reset_settings()
    assert secrets.get("buffer") == "from-file" and "buffer" in secrets.refs()
    monkeypatch.setenv("BUFFER_API_KEY", "from-env")
    assert secrets.get("buffer") == "from-env"
