"""The YouTube path through Ezra's services: connect → approve → publish (job) → post record,
scheduling, retries without duplicates, permanent failures, lifecycle, disconnect.
Runs against the stateful Google fake (tests/youtube_fake.py)."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from ezra import candidates, db, jobs, publishing, render, review, secrets
from ezra.config import reset_settings
from ezra.db.models import AuditEvent, Job, Post
from ezra.publishing import scheduler
from tests.youtube_fake import FakeYouTube, gerr


@pytest.fixture
def fake(monkeypatch, use_processed):
    from ezra.publishing.youtube import YouTubePublisher

    monkeypatch.setattr(YouTubePublisher, "sleep", staticmethod(lambda s: None))
    monkeypatch.setenv("EZRA_YOUTUBE_CHUNK_MB", "1")
    reset_settings()
    f = FakeYouTube()
    publishing.set_http_client(f.client())
    secrets.put("youtube-client", {"client_id": "cid", "client_secret": "csec"})
    yield f
    publishing.set_http_client(None)


def connect(fake: FakeYouTube):
    start = publishing.connect_start("youtube")
    assert "code_challenge" in start["authorize_url"]
    return publishing.connect_finish("youtube", "the-code", start["state"])


def approved_clip() -> int:
    clip = render.render_candidate(candidates.list_candidates("demo", top=1)[0].id)
    review.approve(clip.id, actor="test")
    return clip.id


def run_jobs(n: int = 5) -> list[Job]:
    done = []
    for _ in range(n):
        with db.session() as s:
            s.execute(update(Job).where(Job.status == "queued").values(          # skip backoff waits
                run_after=datetime.now(UTC) - timedelta(seconds=1)))
        job = jobs.claim()
        if job is None:
            break
        done.append(jobs.run(job))
    return done


def test_connect_then_private_upload_end_to_end(fake):
    acc = connect(fake)
    assert acc.handle == "Ezra Test" and acc.meta["channel_id"] == "UC123"
    health = publishing.account_health(acc.id, check_live=True)
    assert health["can_upload"] and health["can_read_analytics"] and not health["can_manage"]
    assert health["mode"] == "private only" and health["live_check"]["ok"]
    assert "access_token" not in str(health) and "rt-1" not in str(health)
    clip_id = approved_clip()
    dry = publishing.publish_clip(clip_id, ["youtube"], visibility="private")
    assert dry["dry_run"] and not dry["problems"]
    res = publishing.publish_clip(clip_id, ["youtube"], visibility="private", confirm=True)
    run_jobs()
    post = publishing.get_post(res["post_ids"][0])
    assert post.status == "published" and post.external_id == "vid1" and post.platform_state == "processing"
    assert post.url == "https://www.youtube.com/shorts/vid1" and post.upload_state["video_id"] == "vid1"
    assert "vid1" in fake.thumbnails and not post.warnings
    body = fake.init_bodies[0]
    assert body["status"]["privacyStatus"] == "private" and body["snippet"]["title"]
    d = publishing.post_dict(post)
    assert d["platform_state"] == "processing" and "upload_state" not in d
    with db.session() as s:
        q = select(AuditEvent).where(AuditEvent.entity_type == "post", AuditEvent.entity_id == str(post.id))
        kinds = {e.action for e in s.scalars(q)}
    assert {"post.created", "post.upload_started", "post.published"} <= kinds


def test_public_is_refused_in_private_only_mode(fake):
    connect(fake)
    dry = publishing.publish_clip(approved_clip(), ["youtube"], visibility="public")
    assert any("audit" in p for p in dry["problems"]) and not dry.get("dry_run")


def test_a_lost_response_is_recovered_by_the_retry_not_duplicated(fake):
    connect(fake)
    fake.lose_final_response = True
    res = publishing.publish_clip(approved_clip(), ["youtube"], visibility="private", confirm=True)
    run_jobs()
    post = publishing.get_post(res["post_ids"][0])
    # recovered by the upload's own status query, before the job even needed a retry
    assert post.status == "published" and post.external_id == "vid1"
    assert len(fake.videos) == 1 and len(fake.sessions) == 1


def test_quota_exhaustion_fails_once_without_retries(fake):
    connect(fake)
    fake.init_error = gerr(403, "quotaExceeded", "you have exceeded your quota")
    res = publishing.publish_clip(approved_clip(), ["youtube"], visibility="private", confirm=True)
    ran = run_jobs()
    assert len(ran) == 1 and ran[0].status == "failed"
    post = publishing.get_post(res["post_ids"][0])
    assert post.status == "failed" and post.error_code == "quota_exceeded"
    assert publishing.post_dict(post)["error_code"] == "quota_exceeded"


def test_transient_failures_end_as_failed_after_the_last_attempt(fake):
    connect(fake)
    fake.init_error = gerr(503, "backendError")
    res = publishing.publish_clip(approved_clip(), ["youtube"], visibility="private", confirm=True)
    ran = run_jobs(10)
    assert [j.status for j in ran][-1] == "failed" and len(ran) == 3        # job_max_attempts
    post = publishing.get_post(res["post_ids"][0])
    assert post.status == "failed" and post.error_code == "server_error" and post.retries == 3


def test_a_revoked_token_marks_the_account_for_reconnect(fake):
    acc = connect(fake)
    fake.revoked = True
    tok = secrets.get(acc.credential_ref)
    secrets.put(acc.credential_ref, {**tok, "expires_at": time.time() - 60})
    clip_id = approved_clip()
    res = publishing.publish_clip(clip_id, ["youtube"], visibility="private", confirm=True)
    run_jobs()
    assert publishing.get_post(res["post_ids"][0]).error_code == "auth_revoked"
    assert publishing.list_accounts("youtube")[-1].status == "reconnect_required"
    again = publishing.publish_clip(clip_id, ["youtube"], visibility="private")
    assert any("reconnected" in p for p in again["problems"])


def test_scheduled_upload_goes_up_now_and_youtube_publishes_it(fake, monkeypatch):
    monkeypatch.setenv("EZRA_YOUTUBE_PUBLIC_ALLOWED", "1")
    reset_settings()
    connect(fake)
    when = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    res = publishing.publish_clip(approved_clip(), ["youtube"], schedule_at=when.isoformat(), confirm=True)
    assert res["job_ids"], "YouTube uploads immediately and schedules platform-side"
    run_jobs()
    post = publishing.get_post(res["post_ids"][0])
    assert post.status == "scheduled" and post.platform_state == "scheduled"
    assert fake.init_bodies[0]["status"]["publishAt"] == when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with db.session() as s:
        s.execute(update(Post).where(Post.id == post.id).values(scheduled_at=datetime.now(UTC) - timedelta(minutes=1)))
    assert scheduler.tick()["queued_posts"] == []           # never re-uploaded by Ezra's scheduler
    assert publishing.refresh_statuses() == []               # YouTube hasn't flipped it yet
    fake.videos["vid1"]["status"].update(privacyStatus="public", uploadStatus="processed")
    fake.videos["vid1"]["status"].pop("publishAt")
    out = publishing.refresh_statuses()
    assert out[0]["status"] == "published" and publishing.get_post(post.id).platform_state == "public"
    assert len(fake.sessions) == 1


def test_cancel_reschedule_update_delete_and_disconnect(fake, monkeypatch):
    monkeypatch.setenv("EZRA_YOUTUBE_PUBLIC_ALLOWED", "1")
    monkeypatch.setenv("EZRA_YOUTUBE_MANAGE", "1")
    reset_settings()
    fake.scope += " https://www.googleapis.com/auth/youtube.force-ssl"
    acc = connect(fake)
    res = publishing.publish_clip(approved_clip(), ["youtube"], schedule_at=(datetime.now(UTC) + timedelta(days=1))
                                  .isoformat(), confirm=True)
    run_jobs()
    pid = res["post_ids"][0]
    later = datetime.now(UTC) + timedelta(days=4)
    publishing.reschedule_post(pid, later.isoformat())
    assert fake.videos["vid1"]["status"]["publishAt"].startswith(later.strftime("%Y-%m-%dT%H:%M"))
    publishing.cancel_post(pid)
    assert "publishAt" not in fake.videos["vid1"]["status"]
    assert publishing.get_post(pid).platform_state == "private"
    assert publishing.update_post(pid, title="Better title")["dry_run"]
    publishing.update_post(pid, title="Better title", confirm=True)
    assert fake.videos["vid1"]["snippet"]["title"] == "Better title"
    assert publishing.delete_post(pid)["dry_run"] and "vid1" in fake.videos
    publishing.delete_post(pid, confirm=True)
    assert "vid1" not in fake.videos and publishing.get_post(pid).status == "deleted"
    out = publishing.disconnect_account(acc.id)
    assert out["token_revoked"] and fake.revoke_calls == 1 and secrets.get(acc.credential_ref) is None
    assert publishing.account_health(acc.id)["status"] == "disconnected"


def test_cli_connect_over_a_loopback_listener(fake):
    import socket
    import threading

    import httpx

    from ezra.publishing import loopback

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    shown: list[str] = []
    result: dict = {}
    def run():
        try:
            result["acc"] = loopback.connect("youtube", port, open_browser=False, timeout=20, show=shown.append)
        except Exception as e:           # surfaced by the asserts below
            result["error"] = e
            shown.append("failed")

    t = threading.Thread(target=run)
    t.start()
    for _ in range(600):
        if shown:
            break
        time.sleep(0.05)
    assert "error" not in result, result.get("error")
    url = shown[0].split("\n")[-1]
    q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert q["redirect_uri"].startswith("http%3A%2F%2F127.0.0.1")
    for _ in range(50):                                     # the listener may not be up yet
        try:
            page = httpx.get(f"http://127.0.0.1:{port}/callback", params={"code": "c", "state": q["state"]})
            break
        except httpx.ConnectError:
            time.sleep(0.1)
    t.join(10)
    assert page.status_code == 200 and "Connected" in page.text
    assert result["acc"].handle == "Ezra Test" and result["acc"].meta["channel_id"] == "UC123"


def test_api_exposes_health_post_state_and_structured_errors(fake, monkeypatch):
    from fastapi.testclient import TestClient

    from ezra.api import app as api_mod

    monkeypatch.setenv("EZRA_API_TOKEN", "tok")
    reset_settings()
    publishing.set_http_client(fake.client())
    c, h = TestClient(api_mod.app), {"Authorization": "Bearer tok"}
    acc = connect(fake)
    health = c.get(f"/api/accounts/{acc.id}/health", headers=h).json()
    assert health["can_upload"] and health["mode"] == "private only" and "rt-1" not in str(health)
    assert c.post(f"/api/accounts/{acc.id}/test-upload", json={}, headers=h).json()["dry_run"]
    clip_id = approved_clip()
    res = c.post(f"/api/clips/{clip_id}/publish", json={"platforms": ["youtube"], "visibility": "private",
                                                        "confirm": True}, headers=h).json()
    run_jobs()
    post = c.get(f"/api/posts/{res['post_ids'][0]}", headers=h).json()
    assert post["platform_state"] == "processing" and post["upload_progress"] == 1.0
    assert c.post(f"/api/posts/{post['id']}/delete", json={}, headers=h).json()["dry_run"]
    r = c.post(f"/api/posts/{post['id']}/update", json={"title": "x", "confirm": True}, headers=h)
    assert r.status_code == 409 and r.json()["error_code"] == "scope_missing" and r.json()["reconnect"]
    out = c.post(f"/api/accounts/{acc.id}/disconnect", headers=h).json()
    assert out["status"] == "disconnected"


def test_test_upload_sends_a_private_pattern_and_can_clean_up(fake, monkeypatch):
    monkeypatch.setenv("EZRA_YOUTUBE_MANAGE", "1")
    reset_settings()
    fake.scope += " https://www.googleapis.com/auth/youtube.force-ssl"
    acc = connect(fake)
    out = publishing.test_private_upload(acc.id, confirm=True, delete_after=True)
    assert out["verified_on_youtube"] and out["privacy"] == "private" and out["deleted"]
    assert fake.init_bodies[0]["status"]["privacyStatus"] == "private" and not fake.videos
