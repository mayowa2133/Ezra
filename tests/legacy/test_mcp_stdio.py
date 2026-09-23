"""Drive the real MCP server over stdio, the way Claude Code / Codex do."""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _data(result):
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise AssertionError(result.content[0].text)
    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def _flow(home):
    params = StdioServerParameters(command=sys.executable, args=["-m", "clipper.mcp_server"],
                                   env={**os.environ, "CLIPPER_HOME": str(home)})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            call = lambda name, **kw: s.call_tool(name, kw)
            brief = _data(await call("get_brief", campaign="campaign-184"))
            assert brief["rubric"]["dimensions"]["hook"]["weight"] == 0.25
            (src,) = _data(await call("list_sources", campaign="campaign-184"))
            page = _data(await call("read_transcript", source_id=src["id"], max_chars=400))
            assert page["next_start"] is not None and "He lost" in page["text"]
            added = _data(await call("add_candidates", source_id=src["id"], candidates=[
                {"start": 0.2, "end": 16.8, "title": "He lost $400k overnight", "hook_type": "loss",
                 "hook_text": "He lost $400k overnight 😳"},
                {"start": 5.8, "end": 16.8, "title": "Nobody tells founders this", "hook_type": "contrarian"}]))
            ids = [a["clip_id"] for a in added]
            ranked = _data(await call("score_clips", scores=[
                {"clip_id": ids[0], "hook": 95, "retention": 90, "context": 85, "emotion": 90,
                 "novelty": 80, "comment": 85, "campaign_fit": 90, "notes": "conflict in first second"},
                {"clip_id": ids[1], "hook": 70, "retention": 60, "context": 50, "emotion": 50,
                 "novelty": 60, "comment": 55, "campaign_fit": 80}]))
            assert ranked[0]["clip_id"] == ids[0] and ranked[0]["ai_score"] > ranked[1]["ai_score"]
            job = _data(await call("render_clips", campaign="campaign-184", top_n=1))
            for _ in range(120):
                st = _data(await call("job_status", job_id=job["job_id"]))
                if st["status"] != "running":
                    break
                await asyncio.sleep(0.5)
            assert st["status"] == "done", st
            _data(await call("review_clips", clip_ids=[ids[0]], decision="approved"))
            copy = _data(await call("set_clip_copy", clip_id=ids[0], copy={
                "tiktok": {"caption": "He lost $400k overnight 😳 #xyzpod"},
                "youtube": {"title": "How He Lost $400,000 Overnight", "caption": "#xyzpod"}}))
            assert copy["issues"] == []
            dry = _data(await call("publish_clip", clip_id=ids[0], platforms=["tiktok", "youtube"]))
            assert dry["dry_run"] and not dry["published"]


def test_mcp_workflow_over_stdio(campaign, home):
    asyncio.run(_flow(home))
