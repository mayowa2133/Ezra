"""Ezra as an MCP server (official Python SDK). Tools call the same
application services as the API, CLI and dashboard. Long operations are queued
as jobs and executed by a worker embedded in this process (EZRA_MCP_WORKER=0
to rely on an external worker); poll with ezra_job_status.

Judgment can come from the calling agent itself: ezra_list_candidates +
ezra_get_transcript to read, ezra_create_candidate to propose moments,
ezra_score_candidate to score them. Approval and publishing tools act only on
the human's explicit decision; publishing is a dry run unless confirm=true.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from . import (
    analysis,
    campaigns,
    candidates,
    db,
    economics,
    jobs,
    metadata,
    metrics,
    publishing,
    render,
    review,
    runner,
    security,
    sources,
    transcription,
)
from . import analytics as perf

INSTRUCTIONS = """Ezra finds, ranks, renders and publishes short-form clips for performance-paid campaigns,
then learns from results. Workflow:
 1. ezra_list_campaigns / ezra_get_campaign (rules, CPM, thresholds) or ezra_create_campaign.
 2. ezra_add_source (authorized footage) → ezra_analyze_source → poll ezra_job_status.
 3. ezra_find_candidates → poll → ezra_list_candidates (ranked; FAIL excluded). Optionally read
    ezra_get_transcript and propose your own moments with ezra_create_candidate, then score any
    candidate per factor with ezra_score_candidate (you judge; Ezra weights and checks compliance).
 4. ezra_render_top or ezra_render_candidate → poll → ezra_list_clips.
 5. Show the human the clips (title, rank, duration, compliance, expected value) and ASK which to
    approve. ezra_approve_clip / ezra_reject_clip only with the human's explicit choice.
 6. ezra_generate_metadata, then ezra_publish_clip (dry run) → confirm=true only after the human says yes.
 7. ezra_sync_metrics, ezra_campaign_report / ezra_earnings_report, ezra_optimize_campaign.
Scores are ranking estimates, never guarantees of views."""

server = MCPServer(name="ezra", instructions=INSTRUCTIONS)


def _job(j: Any) -> dict[str, Any]:
    return jobs.as_dict(j) | {"note": "poll ezra_job_status(job_id) until status is completed"}


# --- campaigns -------------------------------------------------------------------------------

@server.tool()
def ezra_list_campaigns() -> list[dict[str, Any]]:
    """All campaigns with CPM, thresholds and platform rules."""
    return [{k: v for k, v in campaigns.to_dict(c).items() if k != "rules"} for c in campaigns.list_campaigns()]


@server.tool()
def ezra_get_campaign(campaign: str) -> dict[str, Any]:
    """Campaign detail including every enforceable rule (severity fail/review/info)."""
    return campaigns.to_dict(campaigns.get(campaign))


@server.tool()
def ezra_create_campaign(definition: str, format: str | None = None) -> list[dict[str, Any]]:
    """Create/update campaigns from YAML, JSON or CSV text (see campaigns/demo-campaign.yaml)."""
    return [campaigns.to_dict(c) for c in campaigns.import_text(definition, format, ingest_sources=False)]


# --- sources & analysis ----------------------------------------------------------------------

@server.tool()
def ezra_add_source(path: str, campaign: str | None = None, rights_basis: str = "unknown",
                    title: str | None = None) -> dict[str, Any]:
    """Ingest a local video you are authorized to use (path must be inside EZRA_IMPORT_ROOTS).
    rights_basis: campaign_supplied | owned | licensed | permission | unknown."""
    p = security.safe_import_path(path)
    return sources.to_dict(sources.ingest(p, campaign, title, rights_basis, actor="mcp"))


@server.tool()
def ezra_list_sources(campaign: str | None = None) -> list[dict[str, Any]]:
    return [sources.to_dict(s) for s in sources.list_sources(campaign)]


@server.tool()
def ezra_analyze_source(source_id: int, force: bool = False) -> dict[str, Any]:
    """Queue transcription, diarization, scene/face/silence/topic analysis."""
    sources.get(source_id)
    return _job(jobs.enqueue("analyze_source", {"source_id": source_id, "force": force},
                             dedupe_key=f"analyze-{source_id}"))


@server.tool()
def ezra_get_analysis(source_id: int) -> dict[str, Any]:
    """Summary of analysis products with provider and confidence."""
    return analysis.summary_for(source_id)


@server.tool()
def ezra_get_transcript(source_id: int, start: float = 0.0, max_chars: int = 30000) -> dict[str, Any]:
    """Timestamped, speaker-labelled transcript lines; page with next_start until it is null."""
    return transcription.page(source_id, start, max_chars)


# --- candidates -------------------------------------------------------------------------------

@server.tool()
def ezra_find_candidates(source_id: int, campaign: str | None = None, max_candidates: int = 40) -> dict[str, Any]:
    """Queue candidate generation + ranking for a source (analyzes first if needed)."""
    return _job(jobs.enqueue("find_candidates", {"source_id": source_id, "campaign": campaign,
                                                 "max_candidates": max_candidates}, dedupe_key=f"find-{source_id}"))


@server.tool()
def ezra_list_candidates(campaign: str | None = None, source_id: int | None = None, top: int = 20,
                         include_failed: bool = False) -> list[dict[str, Any]]:
    """Ranked candidates with factor scores, compliance and expected value."""
    return [candidates.to_dict(c) for c in candidates.list_candidates(campaign, source_id, top, include_failed)]


@server.tool()
def ezra_get_candidate(candidate_id: int) -> dict[str, Any]:
    """One candidate in full: transcript, context, explanations, opens_with/ends_with."""
    return candidates.to_dict(candidates.get(candidate_id), detail=True)


@server.tool()
def ezra_create_candidate(source_id: int, start: float, end: float, title: str | None = None,
                          hook: str | None = None, hook_type: str | None = None,
                          reason: str | None = None) -> dict[str, Any]:
    """Propose your own moment. Boundaries snap to word edges; check opens_with/ends_with/warnings."""
    return candidates.to_dict(candidates.create_custom(source_id, start, end, title, hook, hook_type, reason,
                                                       origin="agent"), detail=True)


class FactorScores(BaseModel):
    hook: float = Field(ge=0, le=100)
    retention: float = Field(ge=0, le=100)
    context: float = Field(ge=0, le=100)
    emotion: float = Field(ge=0, le=100)
    novelty: float = Field(ge=0, le=100)
    discussion: float = Field(ge=0, le=100)
    payoff: float = Field(ge=0, le=100)
    visual: float = Field(ge=0, le=100)
    campaign_fit: float = Field(ge=0, le=100)


@server.tool()
def ezra_score_candidate(candidate_id: int, scores: FactorScores, notes: str | None = None,
                         hook: str | None = None, title: str | None = None, hook_type: str | None = None,
                         compliant: bool = True, compliance_notes: str | None = None) -> dict[str, Any]:
    """Your per-factor scores (0-100). Ezra applies campaign weights, performance prior and
    diversity; set compliant=false for judgment-only rule concerns."""
    return candidates.to_dict(candidates.set_agent_scores(candidate_id, scores.model_dump(), notes, hook, title,
                                                          hook_type, compliant, compliance_notes))


@server.tool()
def ezra_rank_candidates(campaign: str | None = None, source_id: int | None = None,
                         use_model: bool = True) -> dict[str, Any]:
    """Queue re-ranking (model critique when EZRA_LLM is configured)."""
    return _job(jobs.enqueue("rank_candidates", {"campaign": campaign, "source_id": source_id,
                                                 "use_model": use_model}))


# --- rendering & clips -------------------------------------------------------------------------

@server.tool()
def ezra_render_candidate(candidate_id: int, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Queue a render. spec keys: aspect (9:16|1:1|16:9|4:5), layout (auto|track|split|blur|center),
    caption_theme (clean|bold|karaoke|cinematic|minimal|high-impact), punch_in, remove_silence,
    remove_fillers, cta_text, brand_kit_id, ..."""
    if candidates.get(candidate_id).compliance_status == "FAIL":
        raise PermissionError("candidate fails campaign compliance")
    return _job(jobs.enqueue("render_candidate", {"candidate_id": candidate_id, "spec": spec}))


@server.tool()
def ezra_render_top(campaign: str | None = None, source_id: int | None = None, top: int = 5,
                    spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Queue renders of the N best publishable, unrendered candidates."""
    return _job(jobs.enqueue("render_top", {"campaign": campaign, "source_id": source_id, "top": top, "spec": spec}))


@server.tool()
def ezra_list_clips(campaign: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    """Clips (status rendered = awaiting human review) with current version details."""
    return [render.clip_dict(c) for c in render.list_clips(campaign, status)]


@server.tool()
def ezra_review_queue(campaign: str | None = None) -> list[dict[str, Any]]:
    """Review cards: scores, explanations, compliance, expected value, video path."""
    return review.queue(campaign)


@server.tool()
def ezra_approve_clip(clip_id: int, notes: str | None = None) -> dict[str, Any]:
    """Record the HUMAN's approval. Only call with clips the human explicitly approved."""
    return render.clip_dict(review.approve(clip_id, actor="mcp-human", notes=notes))


@server.tool()
def ezra_reject_clip(clip_id: int, reason: str | None = None) -> dict[str, Any]:
    """Record the human's rejection."""
    return render.clip_dict(review.reject(clip_id, actor="mcp-human", reason=reason))


@server.tool()
def ezra_update_clip(clip_id: int, title: str | None = None, description: str | None = None,
                     hashtags: list[str] | None = None, platform_metadata: dict[str, Any] | None = None,
                     rerender: dict[str, Any] | None = None) -> dict[str, Any]:
    """Edit metadata; pass `rerender` spec changes (e.g. {"caption_theme": "karaoke"}) to queue a new version."""
    clip = review.update(clip_id, title, description, hashtags, platform_metadata, actor="mcp")
    out = render.clip_dict(clip)
    if rerender:
        out["job"] = _job(jobs.enqueue("rerender_clip", {"clip_id": clip_id, "changes": rerender}))
    return out


@server.tool()
def ezra_generate_metadata(clip_id: int, platforms: list[str] | None = None) -> dict[str, Any]:
    """Per-platform title/caption/hashtags (required hashtags/mentions/CTA enforced) + compliance."""
    return metadata.generate(clip_id, platforms)


@server.tool()
def ezra_export_clip(clip_id: int) -> dict[str, Any]:
    """Export an approved clip (mp4, thumbnail, SRT, ASS, JSON) under $EZRA_HOME/exports."""
    from .config import get_settings

    return render.export_clip(clip_id, get_settings().home / "exports" / f"clip-{clip_id}")


# --- publishing & performance --------------------------------------------------------------------

@server.tool()
def ezra_publish_clip(clip_id: int, platforms: list[str] | None = None, visibility: str = "public",
                      confirm: bool = False) -> dict[str, Any]:
    """Publish an approved clip. Without confirm=true this is a dry run showing exactly what
    would be posted and any blocking problems. Set confirm=true only after the human says yes."""
    return publishing.publish_clip(clip_id, platforms, visibility=visibility, confirm=confirm, actor="mcp")


@server.tool()
def ezra_schedule_clip(clip_id: int, schedule_at: str, timezone: str = "UTC", platforms: list[str] | None = None,
                       visibility: str = "public", confirm: bool = False) -> dict[str, Any]:
    """Schedule a post (ISO datetime in `timezone`); dry run unless confirm=true."""
    return publishing.publish_clip(clip_id, platforms, visibility=visibility, schedule_at=schedule_at, tz=timezone,
                                   confirm=confirm, actor="mcp")


def _latest(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    return history[-1] if history else None


@server.tool()
def ezra_list_posts(campaign: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    """Posts with status, URL and latest metrics."""
    out = []
    for p in publishing.list_posts(campaign, status):
        d = publishing.post_dict(p)
        d["latest_metrics"] = _latest(metrics.history(p.id))
        out.append(d)
    return out


@server.tool()
def ezra_sync_metrics(campaign: str | None = None) -> dict[str, Any]:
    """Queue a metrics pull from each platform API."""
    cid = campaigns.get(campaign).id if campaign else None
    return _job(jobs.enqueue("sync_metrics", {"campaign_id": cid}, dedupe_key="sync-metrics"))


@server.tool()
def ezra_record_metrics(post_id: int, views: int, likes: int | None = None, comments: int | None = None,
                        shares: int | None = None, saves: int | None = None) -> dict[str, Any]:
    """Manual snapshot (numbers read off a dashboard); provenance 'manual'."""
    return metrics.snapshot_dict(metrics.record(post_id, views=views, likes=likes, comments=comments,
                                                shares=shares, saves=saves))


@server.tool()
def ezra_campaign_report(campaign: str) -> dict[str, Any]:
    """Earnings, costs, margin, and actionable performance observations."""
    c = campaigns.get(campaign)
    e = economics.earnings(campaign)
    return {"earnings": e, "insights": perf.insights(c.id), "review_pending": len(review.queue(campaign))}


@server.tool()
def ezra_earnings_report(campaign: str) -> dict[str, Any]:
    """Qualified views, estimated vs confirmed revenue, processing cost and margin."""
    return economics.earnings(campaign)


@server.tool()
def ezra_optimize_campaign(campaign: str, apply: bool = False) -> dict[str, Any]:
    """Weight suggestions learned from this campaign's results (apply=true writes them)."""
    return runner.optimize(campaign, apply)


@server.tool()
def ezra_run_campaign(campaign: str, render_top: int = 5, max_candidates: int = 40) -> dict[str, Any]:
    """Queue the full supervised loop: analyze → candidates → rank → render top N → review queue."""
    c = campaigns.get(campaign)
    return _job(jobs.enqueue("run_campaign", {"campaign": c.slug, "render_top": render_top,
                                              "max_candidates": max_candidates}, dedupe_key=f"run-{c.slug}"))


@server.tool()
def ezra_job_status(job_id: int, logs: bool = False) -> dict[str, Any]:
    """Status, progress, result or structured error of a job."""
    return jobs.as_dict(jobs.get(job_id), with_logs=logs)


@server.tool()
def ezra_retry_job(job_id: int) -> dict[str, Any]:
    return jobs.as_dict(jobs.retry(job_id))


def main() -> None:
    db.migrate()
    if os.environ.get("EZRA_MCP_WORKER", "1") != "0":
        from . import worker

        threading.Thread(target=worker.run_forever, name="ezra-mcp-worker", daemon=True).start()
    server.run("stdio")


if __name__ == "__main__":
    main()
