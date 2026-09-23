"""End-to-end workflows on real synthetic footage (podcast fixture): the
pipeline services, the HTTP API, the CLI and the MCP server over stdio.
Requires a TTS engine to build the fixture (skipped otherwise)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ezra import (
    analysis,
    candidates,
    economics,
    jobs,
    metadata,
    metrics,
    publishing,
    render,
    review,
    transcription,
    worker,
)
from ezra import analytics as perf
from tests.conftest import needs_tts

pytestmark = needs_tts


def _streams(path: Path) -> dict[str, float]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json",
                          str(path)], capture_output=True, text=True, check=True)
    return {s["codec_type"]: float(s["duration"]) for s in json.loads(out.stdout)["streams"]}


def _first_speech(path: Path) -> float:
    out = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af", "silencedetect=noise=-35dB:d=0.1",
                          "-vn", "-f", "null", "-"], capture_output=True, text=True)
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out.stderr)]
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", out.stderr)]
    if starts and starts[0] <= 0.05 and ends:
        return ends[0]
    return 0.0


def test_pipeline_on_real_footage(use_processed):
    truth = json.loads(Path(use_processed["truth"]).read_text())
    # transcript: word-level timestamps and speakers
    segs = transcription.load_segments(1)
    words = [w for s in segs for w in s.words]
    assert len(words) > 400 and all(w.e >= w.s for w in words)
    assert {w.spk for w in words} == {"S1", "S2"}
    a = analysis.summary_for(1)["products"]
    assert a["speakers"]["n_speakers"] == len(truth["speakers"]) == 2
    assert a["faces"]["detection_rate"] > 0.9 and "two_shot" in a["faces"]["layouts"]
    assert a["scenes"]["scenes"] >= 1 and len(a["topics"]["topics"]) >= 4
    # candidates: at least ten, fully scored, compliance evaluated independently
    cands = candidates.list_candidates("demo", include_failed=True)
    assert len(cands) >= 10
    for c in cands:
        assert c.compliance_status in ("PASS", "REVIEW_REQUIRED", "FAIL")
        assert all(getattr(c, f"{k}_score") is not None for k in ("hook", "retention", "context", "emotion"))
        assert c.expected_value.get("basis") == "prior"
    # render: vertical, tracked, captioned, audio in sync
    top = candidates.list_candidates("demo", top=2)
    clip = render.render_candidate(top[0].id)
    v = render.current_version(clip)
    assert (v.width, v.height) == (1080, 1920) and v.layout_used == "split"
    path = Path(os.environ["EZRA_HOME"]) / "storage" / v.video_key
    st = _streams(path)
    assert abs(st["video"] - st["audio"]) < 0.12, st
    srt = (Path(os.environ["EZRA_HOME"]) / "storage" / v.srt_key).read_text()
    first_cue = srt.split("\n")[1].split(" --> ")[0]
    h, m, rest = first_cue.split(":")
    cue = int(h) * 3600 + int(m) * 60 + float(rest.replace(",", "."))
    assert abs(cue - _first_speech(path)) < 0.4, (cue, _first_speech(path))
    # review → metadata → publish (local export) → metrics → earnings → insights → export
    blocked = publishing.publish_clip(clip.id, ["tiktok"], confirm=True)
    assert not blocked["published"] and any("not approved" in x for x in blocked["problems"])
    review.approve(clip.id, actor="test")
    meta = metadata.generate(clip.id, ["tiktok", "youtube"], use_model=False)
    assert "#founderstories" in meta["metadata"]["tiktok"]["caption"]
    for p in ("tiktok", "youtube"):
        publishing.add_account(p, "local-export", f"demo-{p}")
    dry = publishing.publish_clip(clip.id, ["tiktok", "youtube"])
    assert not dry["problems"] and dry["dry_run"] and not publishing.list_posts()
    res = publishing.publish_clip(clip.id, ["tiktok", "youtube"], confirm=True)
    for jid in res["job_ids"]:
        jobs.run(jobs.claim(jid))
    posts = publishing.list_posts("demo")
    assert {p.status for p in posts} == {"published"} and all(p.features.get("hook_type") for p in posts)
    again = publishing.publish_clip(clip.id, ["tiktok"], confirm=True)
    assert any("duplicate refused" in x for x in again["problems"])
    metrics.record(posts[0].id, views=12000)
    metrics.record(posts[1].id, views=3000)
    e = economics.earnings("demo")
    assert e["qualified_views"] == 12000 and e["gross_estimated"] == 24.0
    assert perf.insights(None)["n_posts"] == 2
    files = render.export_clip(clip.id, Path(os.environ["EZRA_HOME"]) / "exports")
    assert Path(files["mp4"]).exists() and Path(files["srt"]).exists()


def test_platform_variant_and_rerender(use_processed):
    cand = candidates.list_candidates("demo", top=1)[0]
    clip = render.render_candidate(cand.id, {"caption_theme": "karaoke", "layout": "track"})
    clip = render.create_variant(clip.id, "linkedin")
    v = render.current_version(clip)
    assert (v.width, v.height) == (1080, 1080) and v.spec["caption_theme"] == "karaoke" and v.version == 2


def test_api_workflow(use_processed, monkeypatch):
    from fastapi.testclient import TestClient

    from ezra.api import app as api_mod
    from ezra.config import reset_settings

    monkeypatch.setenv("EZRA_API_TOKEN", "tok")
    reset_settings()
    c = TestClient(api_mod.app)
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/campaigns").status_code == 401
    h = {"Authorization": "Bearer tok"}
    assert c.get("/api/campaigns/nope", headers=h).status_code == 404
    cands = c.get("/api/candidates?campaign=demo&top=5", headers=h).json()
    assert len(cands) == 5 and cands[0]["rank_score"] >= cands[-1]["rank_score"]
    detail = c.get(f"/api/candidates/{cands[0]['id']}", headers=h).json()
    assert detail["opens_with"] and "explanations" in detail
    job = c.post(f"/api/candidates/{cands[0]['id']}/render", headers=h, json={}).json()
    assert job["status"] == "queued"
    worker.run_one(job["id"])
    assert c.get(f"/api/jobs/{job['id']}", headers=h).json()["status"] == "completed"
    queue = c.get("/api/review?campaign=demo", headers=h).json()
    card = queue[0]
    assert card["video_url"] and card["compliance"]["status"] in ("PASS", "REVIEW_REQUIRED")
    media = card["video_url"].replace("http://localhost:8000", "")
    part = c.get(media, headers={"Range": "bytes=0-1023"})
    assert part.status_code == 206 and len(part.content) == 1024
    assert c.get(media.replace("sig=", "sig=x")).status_code == 403
    r = c.post(f"/api/clips/{card['clip_id']}/approve", headers=h, json={"reviewer": "api-test"})
    assert r.json()["status"] == "approved"
    c.post("/api/accounts", headers=h, json={"platform": "youtube", "provider": "local-export", "handle": "yt"})
    dry = c.post(f"/api/clips/{card['clip_id']}/publish", headers=h, json={"platforms": ["youtube"]}).json()
    assert dry["dry_run"] and not dry["problems"]
    bad = c.post(f"/api/clips/{card['clip_id']}/publish", headers=h, json={"platforms": ["linkedin"],
                                                                            "confirm": True}).json()
    assert bad["problems"]
    up = c.post("/api/sources", headers=h, files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert up.status_code == 415
    assert c.get("/api/campaigns/demo/earnings", headers=h).json()["campaign"] == "demo"
    assert c.get("/api/dashboard", headers=h).json()["review_pending"] == 0


def test_cli_workflow(use_processed):
    from typer.testing import CliRunner

    from ezra.cli import app

    r = CliRunner()
    assert "demo" in r.invoke(app, ["campaign", "list"]).output
    out = r.invoke(app, ["candidates", "--campaign", "demo", "--top", "3"])
    assert out.exit_code == 0 and "compliance" in out.output and ("PASS" in out.output or "REVIEW" in out.output)
    cid = candidates.list_candidates("demo", top=1)[0].id
    out = r.invoke(app, ["render", "--candidate", str(cid)])
    assert out.exit_code == 0, out.output
    clip_id = render.list_clips("demo")[0].id
    assert r.invoke(app, ["approve", str(clip_id)]).exit_code == 0
    assert r.invoke(app, ["accounts", "add", "tiktok", "local-export", "me"]).exit_code == 0
    out = r.invoke(app, ["publish", "--clip", str(clip_id), "--platforms", "tiktok", "--yes"])
    assert out.exit_code == 0 and "published" in out.output, out.output
    assert r.invoke(app, ["report", "demo"]).exit_code == 0
    bad = r.invoke(app, ["approve", "9999"])
    assert bad.exit_code != 0


async def _mcp_flow(home: str) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "EZRA_HOME": home, "EZRA_JOB_ISOLATION": "false"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "ezra.mcp_server"], env=env)

    def data(res):
        assert not getattr(res, "is_error", False), res.content[0].text
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc

    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        required = {"ezra_create_campaign", "ezra_get_campaign", "ezra_list_campaigns", "ezra_add_source",
                    "ezra_analyze_source", "ezra_find_candidates", "ezra_list_candidates", "ezra_rank_candidates",
                    "ezra_render_candidate", "ezra_render_top", "ezra_list_clips", "ezra_approve_clip",
                    "ezra_reject_clip", "ezra_update_clip", "ezra_generate_metadata", "ezra_publish_clip",
                    "ezra_schedule_clip", "ezra_list_posts", "ezra_sync_metrics", "ezra_campaign_report",
                    "ezra_earnings_report", "ezra_optimize_campaign", "ezra_run_campaign"}
        assert required <= names, required - names
        call = lambda n, **kw: s.call_tool(n, kw)
        cands = data(await call("ezra_list_candidates", campaign="demo", top=3))
        seg = data(await call("ezra_get_transcript", source_id=1, max_chars=500))
        assert seg["next_start"] is not None
        scored = data(await call("ezra_score_candidate", candidate_id=cands[1]["id"], scores={
            "hook": 95, "retention": 90, "context": 90, "emotion": 88, "novelty": 80, "discussion": 85,
            "payoff": 90, "visual": 70, "campaign_fit": 92}, notes="agent judgement"))
        assert scored["scorer"] == "agent+heuristic" and scored["rank_score"] > 80
        job = data(await call("ezra_render_candidate", candidate_id=cands[1]["id"]))
        for _ in range(240):
            st = data(await call("ezra_job_status", job_id=job["id"]))
            if st["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.5)
        assert st["status"] == "completed", st
        clip_id = st["result"]["clip_id"]
        data(await call("ezra_approve_clip", clip_id=clip_id))
        dry = data(await call("ezra_publish_clip", clip_id=clip_id, platforms=["tiktok"]))
        assert dry["problems"] and "no tiktok account" in dry["problems"][0]


def test_mcp_workflow_over_stdio(use_processed):
    asyncio.run(_mcp_flow(os.environ["EZRA_HOME"]))
