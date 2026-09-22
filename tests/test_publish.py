from clipper import revenue
from clipper.clipping import clips
from clipper.clipping.clips import Candidate, PlatformCopy
from clipper.publishing import publisher
from clipper.ranking.scoring import ClipScore


class FakeUploadPost:
    def __init__(self):
        self.uploads = []

    def upload(self, video, platforms, copy, scheduled_date=None, timezone=None):
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
