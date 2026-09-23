import math
from datetime import timedelta

import pytest

from ezra import analytics, campaigns, db, economics, experiments, metrics
from ezra.db.models import Candidate, Clip, ClipVersion, Post, Source, utcnow
from tests.conftest import DEMO_YAML


def _seed_posts(views: list[int], features: list[dict] | None = None, variant: list[str] | None = None,
                experiment_id: int | None = None) -> list[int]:
    camp = campaigns.get("demo")
    ids = []
    with db.session() as s:
        src = Source(campaign_id=camp.id, title="x", storage_key="k", sha256=f"h{len(views)}{experiment_id}",
                     duration=100)
        s.add(src)
        s.flush()
        for i, _ in enumerate(views):
            cand = Candidate(campaign_id=camp.id, source_id=src.id, start=0, end=30, rank_score=40 + i)
            s.add(cand)
            s.flush()
            clip = Clip(candidate_id=cand.id, campaign_id=camp.id, source_id=src.id, status="published")
            s.add(clip)
            s.flush()
            ver = ClipVersion(clip_id=clip.id, version=1, spec={})
            s.add(ver)
            s.flush()
            f = {"duration": 30, "rank_score": 40 + i, "hook_type": "how_to" if i % 2 else "loss"}
            f.update((features or [{}] * len(views))[i])
            p = Post(clip_id=clip.id, clip_version_id=ver.id, campaign_id=camp.id, platform="tiktok",
                     provider="local-export", status="published", visibility="public",
                     idempotency_key=f"k{i}-{experiment_id}", features=f, published_at=utcnow() - timedelta(days=2),
                     experiment_id=experiment_id, experiment_variant=(variant or [None] * len(views))[i])
            s.add(p)
            s.flush()
            ids.append(p.id)
    for pid, v in zip(ids, views):
        metrics.record(pid, views=v)
    return ids


def test_payout_rules():
    (c,) = campaigns.import_text(DEMO_YAML)
    assert economics.payout(4999, c)["estimated"] == 0
    assert economics.payout(12000, c)["estimated"] == 24.0
    assert economics.payout(900000, c) == {"qualified_views": 900000, "estimated": 1000, "capped": True,
                                           "eligible": True}
    assert economics.payout(900000, c, "linkedin")["estimated"] == 0   # platform not in campaign


def test_earnings_budget_tracking_window_and_confirmed():
    campaigns.import_text(DEMO_YAML)
    ids = _seed_posts([12000, 3000, 900000, 700000])
    econ = economics.earnings("demo")
    assert econ["qualified_views"] == 12000 + 900000 + 700000 and econ["qualifying_posts"] == 3
    assert econ["gross_estimated"] == 1500 and econ["budget_capped"]      # 24 + 1000 + 1000 capped at budget
    # a snapshot captured after the 30-day window must not count
    with db.session() as s:
        s.get(Post, ids[0]).published_at = utcnow() - timedelta(days=40)
    metrics.record(ids[0], views=999999)
    assert next(p for p in economics.earnings("demo")["posts"] if p["post_id"] == ids[0])["views"] == 0
    economics.record_revenue("demo", 55.5, ids[2])
    assert economics.earnings("demo")["confirmed"] == 55.5


def test_expected_value_is_monotonic_and_honest():
    (c,) = campaigns.import_text(DEMO_YAML)
    lo, hi = economics.expected_value(c, 30), economics.expected_value(c, 90)
    assert hi["ev_per_post"] > lo["ev_per_post"] and hi["p_qualify"] > lo["p_qualify"]
    assert hi["basis"] == "prior" and hi["p10"] <= hi["ev_per_render"] + 1e-9 <= hi["p90"] + 1000
    _seed_posts([20000, 30000, 50000, 8000, 9000])
    m = economics.view_model(c.id)
    assert m["n"] == 5 and m["basis"] == "5 observed posts" and m["mu"] > economics.PRIOR_MU


def test_insights_cohorts_calibration_and_prior():
    campaigns.import_text(DEMO_YAML)
    views = [80000, 2000, 90000, 3000, 70000, 2500, 85000, 1500, 60000, 2200, 95000, 1800, 88000, 2400, 76000, 2100]
    _seed_posts(views)
    ins = analytics.insights(campaigns.get("demo").id)
    assert ins["n_posts"] == 16
    loss = next(r for r in ins["cohorts"] if r["trait"] == "hook_type" and r["value"] == "loss")
    assert loss["shrunk_multiplier"] > 1 and loss["interval80"][0] > 1
    assert any("hook type = loss" in o and "Favour it" in o for o in ins["observations"])
    assert ins["regression"]["n"] == 16
    hi, conf, basis = analytics.prior({"hook_type": "loss", "duration": 30}, campaigns.get("demo").id)
    lo, _, _ = analytics.prior({"hook_type": "how_to", "duration": 30}, campaigns.get("demo").id)
    assert hi > lo and conf > 0 and "hook_type=loss" in basis


def test_learnings_roundtrip():
    lr = analytics.save_learning("Loss hooks win", {"n": 12})
    assert [x.text for x in analytics.learnings()] == ["Loss hooks win"]
    analytics.retire_learning(lr.id)
    assert analytics.learnings() == []


def test_experiment_analysis_declares_winner_only_with_evidence():
    campaigns.import_text(DEMO_YAML)
    e = experiments.create("demo", "theme test", "caption_theme", ["bold", "karaoke"], min_samples=4)
    with pytest.raises(ValueError):
        experiments.create("demo", "bad", "caption_theme", ["bold", "not-a-theme"])
    _seed_posts([40000, 50000, 45000, 60000, 3000, 2000, 2500, 3500],
                variant=["A", "A", "A", "A", "B", "B", "B", "B"], experiment_id=e.id)
    res = experiments.analyze(e.id)
    a = next(v for v in res["variants"] if v["variant"] == "A")
    assert res["decided"] and a["p_best"] > 0.9 and "variant A" in res["verdict"]
    e2 = experiments.create("demo", "thin", "layout", ["track", "blur"], min_samples=10)
    assert not experiments.analyze(e2.id)["decided"]


def test_optimizer_needs_data_then_suggests_weights():
    from ezra import runner

    campaigns.import_text(DEMO_YAML)
    assert runner.optimize("demo")["suggested_weights"] is None
    _seed_posts([1000 * (i + 1) for i in range(10)],
                features=[{"hook": 10 * i, "retention": 50} for i in range(10)])
    out = runner.optimize("demo")
    assert out["factor_correlation"]["hook"] > 0.9
    assert out["suggested_weights"]["hook"] > out["current_weights"]["hook"]
    assert math.isclose(sum(out["suggested_weights"].values()), 1, abs_tol=0.01)
