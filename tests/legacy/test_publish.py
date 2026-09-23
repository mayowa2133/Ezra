from clipper import revenue
from clipper.clipping import clips
from clipper.clipping.clips import Candidate, PlatformCopy
from clipper.publishing import publisher
from clipper.ranking.scoring import ClipScore


class FakeUploadPost:
    def __init__(self):
        self.uploads = []

    def upload(self, video, platforms, copy, scheduled_date=None, timezone=None, private=False):
        self.uploads.append((video, platforms, copy))
        return {"success": True, "request_id": "req-1"}

    def status(self, request_id):
        return {"results": {
            "instagram": {"success": True, "url": "https://instagram.com/p/abc", "post_id": "ig1"},
            "youtube": {"success": False, "error": "quota"}}}

    def post_analytics(self, platform=None, limit=100):
        return {"instagram": [{"post_id": "ig1", "post_metrics": {"views": 12000, "likes": 800}}]}.get(platform, [])


def _approved_clip(campaign):
    (c,) = clips.add_candidates(campaign["source"]["id"], [Candidate(start=0, end=16.9, title="t")])
    clips.score([ClipScore(clip_id=c["clip_id"], hook=80, retention=80, context=80, emotion=80,
                           novelty=80, comment=80, campaign_fit=80)])
    clips.render_clip(c["clip_id"])
    return c["clip_id"]


def test_publish_gate_and_flow(campaign):
    cid = _approved_clip(campaign)
    fake = FakeUploadPost()
    blocked = publisher.publish(cid, ["instagram", "youtube"], confirm=True, client=fake)
    assert not blocked["published"] and any("not approved" in p for p in blocked["problems"])

    clips.set_review([cid], "approved")
    clips.set_copy(cid, {"instagram": PlatformCopy(caption="One mistake #xyzpod"),
                         "youtube": PlatformCopy(title="How He Lost $400,000", caption="#xyzpod")})
    dry = publisher.publish(cid, ["instagram", "youtube"], client=fake)
    assert dry["dry_run"] and not fake.uploads  # no confirm -> nothing sent

    res = publisher.publish(cid, ["instagram", "youtube"], confirm=True, client=fake)
    assert res["published"] and len(fake.uploads) == 1
    assert {p["status"] for p in res["posts"]} == {"submitted"}

    updated = publisher.refresh_status(fake)
    assert {u["platform"]: u["status"] for u in updated} == {"instagram": "published", "youtube": "failed"}
    assert publisher.sync_metrics(fake) == {"snapshots_stored": 1, "posts_unmatched": 0}
    rep = revenue.report("campaign-184")
    assert rep["total_views"] == 12000 and rep["estimated_revenue"] == 24.0


def _capture_uploadpost():
    import httpx
    from clipper.integrations.uploadpost import UploadPost

    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req.content.decode(errors="ignore"))
        return httpx.Response(200, json={"success": True, "results": {
            "tiktok": {"success": True, "url": "https://tiktok.test/1", "post_id": "t1"},
            "youtube": {"success": True, "url": "https://youtube.test/1", "post_id": "y1"}}})

    up = UploadPost("key", "me")
    up.client = httpx.Client(transport=httpx.MockTransport(handler), headers=up.client.headers)
    return up, sent


def _form_value(body: str, name: str) -> str:
    import re
    m = re.search(rf'name="{re.escape(name)}"\r\n\r\n([^\r]*)', body)
    return m.group(1) if m else ""


def test_private_publish_sends_private_settings_and_stays_out_of_revenue(campaign):
    cid = _approved_clip(campaign)
    clips.set_review([cid], "approved")
    clips.set_copy(cid, {"tiktok": PlatformCopy(caption="test #xyzpod"),
                         "youtube": PlatformCopy(title="test", caption="#xyzpod"),
                         "instagram": PlatformCopy(caption="test #xyzpod")})

    blocked = publisher.publish(cid, ["tiktok", "instagram"], confirm=True, private=True, client=FakeUploadPost())
    assert not blocked["published"] and any("instagram can't post privately" in p for p in blocked["problems"])

    up, sent = _capture_uploadpost()
    res = publisher.publish(cid, ["tiktok", "youtube"], confirm=True, private=True, client=up)
    assert res["visibility"] == "private" and {p["visibility"] for p in res["posts"]} == {"private"}
    assert _form_value(sent[0], "privacy_level") == "SELF_ONLY"
    assert _form_value(sent[0], "privacyStatus") == "private"
    assert clips.get(cid)["status"] == "approved"  # can still be published for real

    from clipper.analytics import metrics
    for p in res["posts"]:
        metrics.record(p["post_id"], views=50_000)
    assert revenue.report("campaign-184")["total_views"] == 0
    assert len(publisher.list_posts("campaign-184")) == 2  # listings still show them

    res = publisher.publish(cid, ["tiktok", "youtube"], confirm=True, client=up)
    assert _form_value(sent[1], "privacy_level") == "PUBLIC_TO_EVERYONE"
    assert _form_value(sent[1], "privacyStatus") == "public"
    assert clips.get(cid)["status"] == "published"


def test_existing_database_gains_visibility_column(home):
    import sqlite3
    home.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(home / "clipper.db")
    old.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL, platform TEXT NOT NULL, "
                "post_id TEXT, post_url TEXT, request_id TEXT, status TEXT NOT NULL DEFAULT 'submitted', "
                "caption TEXT, actual_payout REAL, response_json TEXT, posted_at TEXT NOT NULL)")
    old.execute("INSERT INTO posts (clip_id, platform, posted_at) VALUES (1, 'tiktok', '2026-09-01')")
    old.commit()
    old.close()
    from clipper import db
    with db.connect() as conn:
        row = conn.execute("SELECT visibility FROM posts").fetchone()
    assert row["visibility"] == "public"
