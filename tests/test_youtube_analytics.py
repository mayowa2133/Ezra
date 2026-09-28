"""YouTube Analytics snapshots and the learning layer that reads them."""

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from ezra import analytics, campaigns, db, economics, metrics
from ezra.db.models import Clip, MetricSnapshot, Post, PublishAccount, Source
from ezra.publishing import youtube_analytics
from ezra.publishing.youtube import YouTubePublisher
from tests.conftest import DEMO_YAML
from tests.youtube_fake import FakeYouTube

REAL_SHAPE = {  # a resultTable as the API returns it; columns deliberately not in our request order
    "kind": "youtubeAnalytics#resultTable",
    "columnHeaders": [{"name": "video", "columnType": "DIMENSION"}, {"name": "averageViewPercentage"},
                      {"name": "views"}, {"name": "estimatedMinutesWatched"}, {"name": "subscribersGained"}],
    "rows": [["abc", 87.4, 1200, 410, 3], ["def", 61.0, 300, 55, 0]],
}


def test_parse_uses_the_column_headers_and_normalize_keeps_missing_missing():
    rows = youtube_analytics.parse(REAL_SHAPE)
    assert rows["abc"]["views"] == 1200 and rows["def"]["averageViewPercentage"] == 61.0
    n = youtube_analytics.normalize(rows["abc"])
    assert n == {"views": 1200, "watch_minutes": 410.0, "avg_view_pct": 87.4, "followers_gained": 3}
    assert "shares" not in n and "impressions" not in n        # not reported → not invented


def _post(camp_id: int, acc_id: int, ext: str, features: dict, days_ago: int = 5) -> int:
    with db.session() as s:
        src = s.query(Source).first() or Source(title="s", storage_key="k", sha256="x" * 64, campaign_id=camp_id)
        s.add(src)
        s.flush()
        from ezra.db.models import Candidate

        cand = Candidate(campaign_id=camp_id, source_id=src.id, start=0, end=20, transcript="t", title="t")
        s.add(cand)
        s.flush()
        clip = Clip(candidate_id=cand.id, campaign_id=camp_id, source_id=src.id, title="t", status="published")
        s.add(clip)
        s.flush()
        p = Post(clip_id=clip.id, account_id=acc_id, campaign_id=camp_id, platform="youtube", provider="youtube",
                 external_id=ext, status="published", visibility="public", idempotency_key=f"k-{ext}",
                 published_at=datetime.now(UTC) - timedelta(days=days_ago), features=features)
        s.add(p)
        s.flush()
        return p.id


@pytest.fixture
def camp():
    (c,) = campaigns.import_text(DEMO_YAML)
    return c


def _account(scopes: list[str]) -> int:
    from ezra import secrets

    secrets.put("youtube-client", {"client_id": "cid", "client_secret": "csec"})
    secrets.put("youtube:Ezra Test", {"access_token": "at-1", "refresh_token": "rt-1",
                                      "expires_at": datetime.now(UTC).timestamp() + 3000})
    with db.session() as s:
        a = PublishAccount(platform="youtube", provider="youtube", handle="Ezra Test",
                           credential_ref="youtube:Ezra Test", meta={"channel_id": "UC123", "scopes": scopes})
        s.add(a)
        s.flush()
        return a.id


def test_sync_stores_windowed_snapshots_and_earnings_ignore_them(camp):
    fake = FakeYouTube()
    acc = _account(fake.scope.split())
    p1 = _post(camp.id, acc, "abc", {"duration": 22})
    p2 = _post(camp.id, acc, "def", {"duration": 50})
    metrics.record(p1, provider="youtube-api", views=9000)             # live lifetime count
    fake.analytics = {"abc": {"views": 7800, "averageViewPercentage": 91.5, "estimatedMinutesWatched": 2400,
                              "averageViewDuration": 20, "subscribersGained": 12, "subscribersLost": 2,
                              "likes": 400, "comments": 30, "shares": 25}}
    out = youtube_analytics.sync(pub=YouTubePublisher(fake.client()))
    assert out["snapshots"] == 1 and out["skipped"][0]["post_id"] == p2      # no rows yet: nothing stored
    q = fake.analytics_requests[0]
    assert q["ids"] == ["channel==MINE"] and q["dimensions"] == ["video"] and "averageViewPercentage" in q["metrics"][0]
    with db.session() as s:
        snap = s.query(MetricSnapshot).filter_by(post_id=p1, provider="youtube-analytics").one()
        assert (snap.avg_view_pct, snap.watch_minutes, snap.subscribers_lost) == (91.5, 2400.0, 2)
        assert snap.window_start < snap.window_end and snap.raw["row"]["views"] == 7800
        post = s.get(Post, p1)
    assert economics.counted_views(post, camp)[0] == 9000                # lagging report isn't counted
    assert metrics.history(p1)[-1]["window_start"]


def test_missing_analytics_permission_is_reported_not_fatal(camp):
    fake = FakeYouTube()
    acc = _account(["https://www.googleapis.com/auth/youtube.upload"])
    _post(camp.id, acc, "abc", {})
    out = youtube_analytics.sync(pub=YouTubePublisher(fake.client()))
    assert out["snapshots"] == 0 and "reconnect" in out["skipped"][0]["reason"]


def test_performance_report_speaks_with_n_and_confidence(camp):
    rng = random.Random(3)
    acc = _account([])
    for i in range(24):
        short = i % 2 == 0
        pid = _post(camp.id, acc, f"v{i}", {"duration": 24 if short else 50, "rank_score": 60 + (10 if short else 0),
                                            "hook_type": "danger" if i % 3 else "reveal"})
        metrics.record(pid, provider="youtube-api", views=int(math.exp(rng.gauss(8 if short else 7, 0.4))),
                       likes=50)
        metrics.record(pid, provider="youtube-analytics", avg_view_pct=rng.gauss(85 if short else 60, 5),
                       avg_watch_seconds=20.0, followers_gained=2, subscribers_lost=1)
    rep = analytics.performance_report(camp.id)
    pct = rep["outcomes"]["avg_view_pct"]
    top = next(c for c in pct["comparisons"] if c["trait"] == "duration_bucket")
    assert (top["better"], top["worse"]) == ("20-30s", "45s+") and top["confidence"] in ("moderate", "high")
    assert top["n"] == 24 and top["effect"] > 15
    text = " ".join(rep["statements"])
    assert "duration bucket = 20-30s currently outperforms 45s+" in text and "N=24" in text
    assert "not proof of cause" in text
    assert "qualified_earnings" in rep["outcomes"] and pct["rank_correlation"] > 0.5


def test_weak_evidence_is_labelled_low_and_kept_out_of_statements(camp):
    acc = _account([])
    for i in range(6):
        pid = _post(camp.id, acc, f"w{i}", {"duration": 24 if i % 2 else 50})
        metrics.record(pid, provider="youtube-api", views=1000 + i)
    rep = analytics.performance_report(camp.id)
    comps = rep["outcomes"]["views"]["comparisons"]
    assert all(c["confidence"] == "low" for c in comps)
    assert rep["statements"][0].startswith("With 6 posts no trait clearly separates")


def test_cli_performance_and_youtube_analytics_commands(camp):
    from typer.testing import CliRunner

    from ezra.cli import app

    acc = _account([])
    for i in range(8):
        pid = _post(camp.id, acc, f"c{i}", {"duration": 24 if i % 2 else 50})
        metrics.record(pid, provider="youtube-api", views=1000 * (i + 1))
    r = CliRunner().invoke(app, ["performance", "--campaign", camp.slug])
    assert r.exit_code == 0, r.output
    assert "8 posts with metrics" in r.output and "views: n=8" in r.output
    r = CliRunner().invoke(app, ["metrics", "youtube-analytics", "--help"])
    assert r.exit_code == 0 and "watch time" in r.output
