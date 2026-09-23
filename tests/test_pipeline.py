from rich.console import Console

from clipper import db, pipeline
from clipper.clipping import clips
from clipper.clipping.clips import Candidate, PlatformCopy
from clipper.ranking.scoring import ClipScore


class FakeUploader:
    def upload(self, video, platforms, copy, scheduled_date=None, timezone=None):
        return {"success": True, "results": {p: {"success": True, "url": f"https://{p}.test/1", "post_id": p}
                                             for p in platforms}}


def fake_agent(campaign):
    """Stands in for Claude Code: does over Python what the real agent does over MCP."""
    sid = campaign["source"]["id"]

    def agent(prompt: str) -> str:
        if "clip strategist" in prompt:
            added = clips.add_candidates(sid, [
                Candidate(start=0, end=16.9, title="He lost $400k overnight", hook_type="loss",
                          hook_text="He lost $400k overnight"),
                Candidate(start=28.6, end=45.3, title="Nobody tells founders this", hook_type="contrarian"),
                Candidate(start=11.4, end=28.2, title="I almost quit the company", hook_type="confession"),
            ])
            base = {"retention": 80, "context": 80, "emotion": 80, "novelty": 80, "comment": 80, "campaign_fit": 80}
            clips.score([ClipScore(clip_id=a["clip_id"], hook=h, **base)
                         for a, h in zip(added, (98, 90, 85)) if a["status"] == "candidate"])
            return "top: He lost $400k overnight"
        if "platform copy" in prompt:
            for c in clips.list_clips("campaign-184", "approved"):
                clips.set_copy(c["id"], {
                    "tiktok": PlatformCopy(caption="He lost $400k overnight 😳 #xyzpod"),
                    "instagram": PlatformCopy(caption="One mistake almost cost him everything... #xyzpod"),
                    "youtube": PlatformCopy(title="How He Lost $400,000 Overnight", caption="#xyzpod")})
            return "done"
        raise AssertionError(prompt)

    return agent


def test_full_run(campaign):
    answers = iter(["1", "y"])
    out = Console(record=True, width=120)
    result = pipeline.run("campaign-184", pipeline.RunOptions(render=3, platforms=["tiktok", "instagram", "youtube"]),
                          console=out, agent=fake_agent(campaign), ask=lambda q: next(answers),
                          uploader=FakeUploader())
    text = out.export_text()
    assert "Candidate moments: 3" in text and "Rendered: 2" in text and '1. "He lost $400k overnight"' in text
    assert "✓ Tiktok published" in text and "Tracking enabled" in text
    assert len(result["approved"]) == 1 and result["published"] == result["approved"]
    statuses = sorted(c["status"] for c in clips.list_clips("campaign-184"))
    assert statuses.count("published") == 1 and statuses.count("rejected") >= 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 3


def test_parse_selection():
    assert pipeline.parse_selection("1,2", 3) == [0, 1]
    assert pipeline.parse_selection("1-3", 3) == [0, 1, 2]
    assert pipeline.parse_selection("", 3) == []
    assert pipeline.parse_selection("all", 2) == [0, 1]


def test_status_lines_fit_the_terminal():
    narrow = Console(width=40)
    line = pipeline.fit(narrow, "agent: reading transcript (source 1, from 1234s) and more words")
    assert len(line) <= 34 and line.endswith("…")
    assert pipeline.fit(narrow, "short") == "short"
