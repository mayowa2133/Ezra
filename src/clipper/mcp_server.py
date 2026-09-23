"""clipper as an MCP server: the agent (Claude Code, Codex, ...) is the
strategist, clipper is its hands.

The agent reads transcripts and makes every judgment call: which moments,
how good, what copy. clipper does the deterministic work and enforces the
rules: boundary snapping, compliance checks, score weighting, rendering,
the human-approval gate, publishing, metrics and revenue math.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import campaigns, db, jobs, revenue
from .analytics import insights, metrics
from .clipping import clips, sources
from .clipping.clips import Candidate, PlatformCopy
from .clipping.transcript import for_source, page
from .ranking.rubric import brief
from .ranking.scoring import ClipScore

INSTRUCTIONS = """clipper turns long-form campaign footage into short clips. You are the strategist.

Workflow for a campaign:
 1. get_brief(campaign): rules, rubric, and what past performance taught us. Read it first.
 2. list_sources(campaign). Any source not 'transcribed': transcribe_source, then poll job_status.
 3. read_transcript(source_id), paging with next_start until None. Read the WHOLE episode.
 4. add_candidates(source_id, [...]): about 20 moments per source. Check each returned
    `opens_with` and `ends_with`: if a cut does not open on the hook or stops before the
    payoff (see `warnings`), add a corrected candidate.
 5. score_clips([...]): score every candidate on every rubric dimension, comparing them
    against each other. clipper applies the weights.
 6. render_clips(campaign, top_n=5), then poll job_status.
 7. Show the human the ranked, rendered clips (title, score, duration, why, file path) and
    ASK which to approve. Call review_clips only with the ids the human picked.
 8. set_clip_copy per approved clip, per platform, with the campaign's required hashtags.
 9. publish_clip(confirm=true) only after the human says yes to publishing.
Performance loop: sync_metrics / record_metrics -> performance_insights -> save_learning.
"""

server = MCPServer(name="clipper", instructions=INSTRUCTIONS)


def _clip_view(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "clip_id": c["id"], "status": c["status"], "ai_score": c["ai_score"], "title": c["title"],
        "source_id": c["source_id"], "start": c["start_time"], "end": c["end_time"],
        "duration": round(c["end_time"] - c["start_time"], 1), "hook_type": c["hook_type"],
        "hook_text": c["hook_text"], **clips.edges(c["transcript"]), "origin": c["origin"],
        "compliance_issues": db.loads(c["compliance_issues"], []), "judge_notes": c["judge_notes"],
        "video_path": c["video_path"], "render_error": c["render_error"],
        "has_copy": bool(c["copy_json"]),
    }


# --- campaigns & sources -------------------------------------------------

@server.tool()
def list_campaigns() -> list[dict[str, Any]]:
    """All campaigns with CPM, thresholds and counts."""
    return [{k: c[k] for k in ("id", "slug", "name", "platform", "cpm", "min_views", "max_payout",
                               "budget", "n_sources", "n_clips")} for c in campaigns.list_all()]


@server.tool()
def create_campaign(yaml_text: str) -> dict[str, Any]:
    """Create or update a campaign from YAML (see campaigns/example.yaml for the shape)."""
    spec = campaigns.parse_yaml(yaml_text)
    camp = campaigns.create(spec, base_dir=Path.cwd())
    return {"slug": camp["slug"], "id": camp["id"], "rules": spec.model_dump()}


@server.tool()
def add_source(campaign: str, path_or_url: str, title: str | None = None) -> dict[str, Any]:
    """Attach long-form footage (local path, or a URL if yt-dlp is installed) to a campaign."""
    return sources.add(campaign, path_or_url, title)


@server.tool()
def list_sources(campaign: str) -> list[dict[str, Any]]:
    """Sources for a campaign with transcription status and clip counts."""
    return sources.list_for(campaign)


@server.tool()
def transcribe_source(source_id: int) -> dict[str, Any]:
    """Start local Whisper transcription in the background. Poll job_status / list_sources."""
    from .clipping.transcribe import transcribe

    src = sources.get(source_id)
    if src["status"] == "transcribed":
        return {"source_id": source_id, "status": "transcribed", "note": "already done"}
    return jobs.start("transcribe", lambda: {"segments": len(transcribe(source_id).segments)},
                      source_id=source_id)


@server.tool()
def job_status(job_id: str | None = None) -> Any:
    """Status of one background job, or all jobs if no id is given."""
    return jobs.status(job_id)


@server.tool()
def read_transcript(source_id: int, start: float = 0.0, max_chars: int = 30000) -> dict[str, Any]:
    """Timestamped transcript lines from `start` seconds. Keep calling with `next_start`
    until it is null to read the whole source. The `| 123.45s` in each line is the
    absolute start time to use for candidates."""
    return page(for_source(source_id), start, max_chars)


# --- finding & judging ---------------------------------------------------

@server.tool()
def get_brief(campaign: str, n_candidates: int = 20) -> dict[str, Any]:
    """Campaign rules, the scoring rubric with weights, hook types, and learnings from
    past clip performance. Read before proposing or scoring anything."""
    return brief(campaign, n_candidates)


@server.tool()
def add_candidates(source_id: int, candidates: list[Candidate]) -> list[dict[str, Any]]:
    """Propose clip moments. Boundaries snap to word edges; deterministic campaign checks
    run immediately. Returns each clip_id, its final bounds, the words it opens and ends
    with, and warnings such as an ending that stops mid-sentence."""
    return clips.add_candidates(source_id, candidates)


@server.tool()
def score_clips(scores: list[ClipScore]) -> list[dict[str, Any]]:
    """Score candidates on each rubric dimension (0-100). clipper computes ai_score with the
    campaign weights. Set compliant=false (with notes) for judgment-only rule breaks."""
    return clips.score(scores)


@server.tool()
def list_clips(campaign: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Clips ranked by ai_score. status: candidate, scored, rendered, approved, rejected,
    published, rejected_compliance, render_failed."""
    return [_clip_view(c) for c in clips.list_clips(campaign, status, limit)]


@server.tool()
def get_clip(clip_id: int) -> dict[str, Any]:
    """Full clip record, including its transcript, per-dimension scores and copy."""
    c = clips.get(clip_id)
    return {**_clip_view(c), "transcript": c["transcript"], "rationale": c["rationale"],
            "scores": {k: c[f"{k}_score"] for k in ("hook", "retention", "context", "emotion",
                                                     "novelty", "comment", "campaign_fit")},
            "copy": db.loads(c["copy_json"], {})}


@server.tool()
def render_clips(campaign: str | None = None, clip_ids: list[int] | None = None, top_n: int = 5,
                 framing: str | None = None, crop_x: float = 0.5) -> dict[str, Any]:
    """Render clips to 1080x1920 with burned-in captions, in the background. Either give
    clip_ids, or a campaign to render its top_n scored clips. framing: crop | blur |
    openshorts (reuse OpenShorts' render for OpenShorts-origin clips)."""
    if clip_ids is None:
        if campaign is None:
            raise ValueError("give clip_ids or campaign")
        clip_ids = [c["id"] for c in clips.top_unrendered(campaign, top_n)]
    if not clip_ids:
        return {"note": "nothing to render: no scored clips waiting"}

    def run() -> list[dict[str, Any]]:
        out = []
        for cid in clip_ids:
            try:
                c = clips.render_clip(cid, framing, crop_x)
                out.append({"clip_id": cid, "video_path": c["video_path"]})
            except Exception as e:
                out.append({"clip_id": cid, "error": str(e)[:300]})
        return out

    return jobs.start("render", run, clip_ids=clip_ids)


@server.tool()
def run_openshorts(source_id: int, target_clips: int = 15) -> dict[str, Any]:
    """Send a source through a self-hosted OpenShorts and import its moments as candidates
    (then score them alongside yours). Background job; needs OpenShorts running."""
    from .integrations import openshorts

    return jobs.start("openshorts", lambda: openshorts.run(source_id, target_clips), source_id=source_id)


# --- human review, copy, publishing -------------------------------------

@server.tool()
def review_clips(clip_ids: list[int], decision: str) -> list[dict[str, Any]]:
    """Record the HUMAN's review decision ('approved' or 'rejected'). Only call this with ids
    the human explicitly chose in this conversation; never approve on your own judgment."""
    return clips.set_review(clip_ids, decision)


@server.tool()
def set_clip_copy(clip_id: int, copy: dict[str, PlatformCopy]) -> dict[str, Any]:
    """Platform copy keyed by platform (tiktok, instagram, youtube). Returns any campaign
    issues (e.g. a missing required hashtag); fix and call again until issues is empty."""
    return clips.set_copy(clip_id, copy)


@server.tool()
def publish_clip(clip_id: int, platforms: list[str], confirm: bool = False,
                 scheduled_date: str | None = None, timezone: str | None = None) -> dict[str, Any]:
    """Post an approved clip via Upload-Post. Without confirm=true this is a dry run that
    shows exactly what would be posted. Only set confirm=true after the human says yes."""
    from .publishing import publisher

    return publisher.publish(clip_id, platforms, confirm=confirm,
                             scheduled_date=scheduled_date, timezone=timezone)


# --- performance loop ----------------------------------------------------

@server.tool()
def list_posts(campaign: str | None = None) -> list[dict[str, Any]]:
    """Published posts with their latest views/likes/comments/shares."""
    return [{k: p[k] for k in ("id", "clip_id", "clip_title", "platform", "status", "post_url",
                               "posted_at", "views", "likes", "comments", "shares", "saves",
                               "actual_payout")} for p in revenue.posts_with_latest(campaign)]


@server.tool()
def record_metrics(post_id: int, views: int, likes: int | None = None, comments: int | None = None,
                   shares: int | None = None, saves: int | None = None,
                   actual_payout: float | None = None) -> dict[str, Any]:
    """Store a metrics snapshot for a post (and optionally what the campaign actually paid)."""
    snap = metrics.record(post_id, views, likes, comments, shares, saves)
    if actual_payout is not None:
        metrics.set_payout(post_id, actual_payout)
    return snap


@server.tool()
def sync_metrics() -> dict[str, Any]:
    """Pull the latest per-post analytics from Upload-Post."""
    from .publishing import publisher

    return publisher.sync_metrics()


@server.tool()
def campaign_report(campaign: str) -> dict[str, Any]:
    """Views, qualified views and estimated vs actual revenue for a campaign."""
    rep = revenue.report(campaign)
    rep["posts"] = [{k: p[k] for k in ("id", "clip_title", "platform", "views", "qualified_views",
                                        "estimated", "actual_payout")} for p in rep["posts"]]
    return rep


@server.tool()
def performance_insights(campaign: str | None = None, min_n: int = 3) -> dict[str, Any]:
    """Which clip traits (hook type, opening words, duration, platform, topic) over- or
    under-perform, and whether judge scores predict views."""
    return insights.analyze(campaign, min_n)


@server.tool()
def save_learning(text: str, evidence: str | None = None) -> dict[str, Any]:
    """Persist a lesson (e.g. "'I lost...' openings average 4x the views of 'Here's how...'").
    Active learnings are included in every future get_brief."""
    return insights.save_learning(text, evidence)


@server.tool()
def retire_learning(learning_id: int) -> str:
    """Deactivate a learning that newer data contradicts."""
    insights.retire_learning(learning_id)
    return "retired"


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()

