from datetime import UTC, datetime

import pytest

from ezra import campaigns, compliance, sources
from tests.conftest import DEMO_YAML


def test_yaml_nested_shape_normalizes_and_derives_rules():
    (c,) = campaigns.import_text(DEMO_YAML)
    assert (c.slug, c.cpm, c.min_qualified_views, c.max_payout_per_clip, c.budget) == ("demo", 2.0, 5000, 1000, 1500)
    kinds = {r.kind: r for r in c.rules}
    assert {"duration", "source_rights", "platforms", "profanity", "competitors", "hashtags", "subtitles"} <= set(kinds)
    assert kinds["competitors"].params["names"] == ["Acme Ventures"] and kinds["competitors"].severity == "fail"
    assert c.raw_instructions and "Founder Stories" in c.raw_instructions
    assert abs(sum(campaigns.normalized_weights(c.weights).values()) - 1) < 1e-9


def test_json_and_csv_import():
    (j,) = campaigns.import_text('{"name": "Json Camp", "cpm": 3, "platforms": ["youtube"], "hashtags": ["x"]}')
    assert j.slug == "json-camp" and j.required_hashtags == ["#x"] and j.allowed_platforms == ["youtube"]
    rows = campaigns.import_text("name,cpm,min_views,platforms,hashtags,competitors\n"
                                 "Csv A,1.5,1000,tiktok;youtube,#a;#b,Foo\nCsv B,4,0,instagram,,\n", "csv")
    assert [r.slug for r in rows] == ["csv-a", "csv-b"]
    assert rows[0].allowed_platforms == ["tiktok", "youtube"] and rows[0].competitors == ["Foo"]


def test_invalid_campaigns_are_rejected():
    with pytest.raises(ValueError):
        campaigns.parse('{"name": "x", "platforms": ["myspace"]}')
    with pytest.raises(ValueError):
        campaigns.parse('{"name": "x", "weights": {"virality": 1}}')
    with pytest.raises(LookupError):
        campaigns.get("missing")


def test_reimport_keeps_manual_rules():
    campaigns.import_text(DEMO_YAML)
    campaigns.add_rule("demo", "freeform", {"text": "no dancing"}, "review", "no dancing")
    (c,) = campaigns.import_text(DEMO_YAML)
    assert any(r.origin == "manual" and r.kind == "freeform" for r in c.rules)


def test_file_adapter(tmp_path):
    (tmp_path / "a.yaml").write_text(DEMO_YAML)
    (tmp_path / "b.json").write_text('{"name": "B"}')
    got = campaigns.sync_adapter(campaigns.FileCampaignAdapter(tmp_path))
    assert sorted(c.slug for c in got) == ["b", "demo"]


def test_candidate_stage_compliance():
    (c,) = campaigns.import_text(DEMO_YAML)
    ok = compliance.evaluate(c, "candidate", duration=30, transcript="I lost everything.")
    assert ok["status"] == "PASS"
    bad = compliance.evaluate(c, "candidate", duration=30, transcript="That was shit, Acme Ventures copied us.")
    msgs = " ".join(r["message"] for r in bad["reasons"])
    assert bad["status"] == "FAIL" and "profanity" in msgs and "Acme Ventures" in msgs
    short = compliance.evaluate(c, "candidate", duration=9, transcript="fine")
    assert short["status"] == "FAIL" and "too short" in short["reasons"][0]["message"]


def test_rights_and_review_rules(tiny_video):
    campaigns.import_text(DEMO_YAML + "  forbidden_topics: [politics]\n  rules: [No medical claims]\n")
    c = campaigns.get("demo")
    src = sources.ingest(tiny_video, "demo", rights_basis="unknown")
    r = compliance.evaluate(c, "candidate", duration=30, transcript="A calm story.", source=src)
    assert r["status"] == "REVIEW_REQUIRED"
    assert any("rights" in x["message"] for x in r["reasons"]) and any("medical" in x["message"] for x in r["reasons"])
    sources.set_rights(src.id, "rejected", notes="takedown")
    r = compliance.evaluate(c, "candidate", duration=30, transcript="A calm story.", source=sources.get(src.id))
    assert r["status"] == "FAIL"
    hit = compliance.evaluate(c, "candidate", duration=30, transcript="Let's talk politics.",
                              source=sources.set_rights(src.id, "authorized"))
    assert any("politics" in x["message"] for x in hit["reasons"])


def test_publish_stage_compliance():
    campaigns.import_text(DEMO_YAML.replace("brief:", "cta: Follow for part 2\n  mentions: [podxyz]\n  "
                                            "starts_at: 2026-01-01T00:00:00Z\n  ends_at: 2026-12-31T00:00:00Z\n"
                                            "  posting_limits: {per_day: 2}\n  brief:"))
    c = campaigns.get("demo")
    when = datetime(2026, 6, 1, tzinfo=UTC)
    good = compliance.evaluate(c, "publish", copy_text="Wild story #founderstories @podxyz Follow for part 2",
                               platforms=["tiktok"], when=when, posting_counts={"day_total": 0})
    assert good["status"] == "REVIEW_REQUIRED" or good["status"] == "PASS"
    assert not [r for r in good["reasons"] if r["outcome"] == "fail"]
    bad = compliance.evaluate(c, "publish", copy_text="no tags", platforms=["x"],
                              when=datetime(2027, 1, 5, tzinfo=UTC), posting_counts={"day_total": 2})
    msgs = " ".join(r["message"] for r in bad["reasons"])
    assert bad["status"] == "FAIL"
    for expected in ("platform not allowed", "missing hashtags", "missing mentions", "after campaign end",
                     "daily posting limit"):
        assert expected in msgs, expected


def test_render_stage_compliance():
    campaigns.import_text(DEMO_YAML + "  logo_required: true\n")
    c = campaigns.get("demo")
    r = compliance.evaluate(c, "render", render_spec={"captions": False, "logo_key": None})
    assert r["status"] == "FAIL" and len([x for x in r["reasons"] if x["outcome"] == "fail"]) == 2
    spec = {"captions": True, "logo_key": "brand/x.png"}
    assert compliance.evaluate(c, "render", render_spec=spec)["status"] == "PASS"
