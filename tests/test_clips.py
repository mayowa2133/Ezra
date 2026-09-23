from pathlib import Path

import pytest

from clipper import campaigns, revenue
from clipper.analytics import insights, metrics
from clipper.clipping import clips
from clipper.clipping.clips import Candidate, PlatformCopy
from clipper.ranking.scoring import ClipScore


def _score(clip_id, base, **kw):
    dims = dict(hook=base, retention=base, context=base, emotion=base, novelty=base,
                comment=base, campaign_fit=base)
    dims.update(kw)
    return ClipScore(clip_id=clip_id, **dims)


def test_campaign_yaml_shape(campaign):
    spec = campaigns.spec("campaign-184")
    assert spec.rate.cpm == 2.0
    assert spec.requires_hashtag and spec.requires_subtitles
    assert abs(sum(spec.weights.values()) - 1) < 1e-9
    assert spec.weights["hook"] == pytest.approx(0.25)


def test_candidates_snap_and_compliance(campaign):
    sid = campaign["source"]["id"]
    out = clips.add_candidates(sid, [
        Candidate(start=0.1, end=16.9, title="He lost $400k overnight", hook_type="loss"),
        Candidate(start=0.0, end=5.0, title="too short"),
        Candidate(start=15.0, end=40.0, title="has profanity and competitor"),
    ])
    good, short, bad = out
    assert good["status"] == "candidate" and good["opens_with"].startswith("He lost")
    assert good["start"] == 0.0  # snapped, padded, clamped at 0
    assert short["status"] == "rejected_compliance" and "too short" in short["compliance_issues"][0]
    assert any("profanity" in i for i in bad["compliance_issues"])
    assert any("competitor" in i for i in bad["compliance_issues"])


def test_scoring_weights_and_ranking(campaign):
    sid = campaign["source"]["id"]
    a, b = clips.add_candidates(sid, [
        Candidate(start=0, end=16.9, title="a"), Candidate(start=29, end=50, title="b")])
    ranked = clips.score([_score(a["clip_id"], 60, hook=100), _score(b["clip_id"], 70)])
    # a = 0.25*100 + 0.75*60 = 70 ; b = 70 -> tie broken by sort stability; check exact values
    by_id = {r["clip_id"]: r for r in ranked}
    assert by_id[a["clip_id"]]["ai_score"] == 70.0
    assert by_id[b["clip_id"]]["ai_score"] == 70.0
    flagged = clips.score([ClipScore(**{**_score(b["clip_id"], 90).model_dump(), "compliant": False,
                                        "compliance_notes": "creator not visible"})])
    assert flagged[0]["status"] == "rejected_compliance"


def test_render_review_copy_revenue_insights(campaign):
    sid = campaign["source"]["id"]
    (c,) = clips.add_candidates(sid, [Candidate(start=0, end=18, title="He lost it all",
                                                hook_text="He lost $400k overnight 😳")])
    clips.score([_score(c["clip_id"], 80)])
    with pytest.raises(ValueError):
        clips.set_review([c["clip_id"]], "approved")  # not rendered yet
    rendered = clips.render_clip(c["clip_id"])
    path = Path(rendered["video_path"])
    assert path.exists() and path.stat().st_size > 10_000
    from clipper.clipping.sources import probe_duration
    assert abs(probe_duration(path) - (rendered["end_time"] - rendered["start_time"])) < 0.3

    clips.set_review([c["clip_id"]], "approved")
    res = clips.set_copy(c["clip_id"], {"tiktok": PlatformCopy(caption="He lost $400k overnight 😳")})
    assert res["issues"] and "#xyzpod" in res["issues"][0]
    res = clips.set_copy(c["clip_id"], {"tiktok": PlatformCopy(caption="He lost $400k overnight 😳 #xyzpod")})
    assert res["issues"] == []

    from clipper import db
    with db.connect() as conn:
        pid = conn.execute("INSERT INTO posts (clip_id, platform, posted_at) VALUES (?,?,?)",
                           (c["clip_id"], "tiktok", db.now())).lastrowid
    metrics.record(pid, views=4000)
    assert revenue.report("campaign-184")["estimated_revenue"] == 0  # below minimum_views
    metrics.record(pid, views=84_300, likes=5000)
    rep = revenue.report("campaign-184")
    assert rep["qualified_views"] == 84_300 and rep["estimated_revenue"] == pytest.approx(168.6)
    metrics.record(pid, views=900_000)
    assert revenue.report("campaign-184")["estimated_revenue"] == 1000  # maximum_payout cap

    a = insights.analyze(min_n=1)
    assert a["n_posts"] == 1 and a["patterns"]
    insights.save_learning("Loss hooks beat how-to hooks 3:1", "n=12")
    assert insights.for_brief()["learnings"] == ["Loss hooks beat how-to hooks 3:1"]


def test_spearman():
    assert insights.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert insights.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0


def test_join_words_glues_split_tokens():
    from clipper.clipping.transcript import join_words
    assert join_words(["I", "lost", "$400", ",000", "overnight,", "40", "%", "of", "it."]) == \
        "I lost $400,000 overnight, 40% of it."


def test_caption_overlays_never_overlap():
    """Adjacent captions share a boundary; overlay windows must be half-open."""
    from pathlib import Path
    from clipper.clipping.render import build_command
    cmd = build_command("in.mp4", Path("out.mp4"), 0, 10, "crop", 0.5,
                        [(Path("a.png"), 0.0, 1.5, 100), (Path("b.png"), 1.5, 3.0, 100)])
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "between(" not in graph and "lt(t,1.500)" in graph and "gte(t,1.500)" in graph


def test_edges_flag_mid_sentence_endings(campaign):
    from clipper.clipping.clips import edges
    assert edges("I almost quit. Talk to the one person who believes")["warnings"]
    ok = edges("Talk to the one person who believes in the company more than you do.")
    assert ok["warnings"] == [] and ok["ends_with"].endswith("more than you do.")
    short, full = clips.add_candidates(campaign["source"]["id"], [
        Candidate(start=0, end=16.9, title="stops one word early"),
        Candidate(start=0, end=17.4, title="complete sentence")])
    assert short["ends_with"].endswith("product market") and short["warnings"]
    assert full["ends_with"].endswith("market fit.") and full["warnings"] == []


def test_resegment_splits_long_asr_segments_into_sentences():
    from clipper.clipping.transcript import resegment
    words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3} for i, w in enumerate(
        "I lost it all. Then I rebuilt it slowly".split())]
    segs = resegment(words)
    assert [s["text"] for s in segs] == ["I lost it all.", "Then I rebuilt it slowly"]
    assert segs[1]["start"] == words[4]["s"]
    gap = [{"w": "one", "s": 0, "e": 0.3}, {"w": "two", "s": 2.0, "e": 2.3}]
    assert len(resegment(gap)) == 2  # a long pause also breaks a segment
