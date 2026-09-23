"""Built-in job tasks. Each is a thin wrapper around an application service,
so the queue, API, CLI and MCP run the same code paths."""

from __future__ import annotations

from typing import Any

from .jobs import JobContext, task


@task("noop")
def noop(ctx: JobContext) -> dict[str, Any]:
    """Health check / test task: optionally fails `fail_times` times first."""
    ctx.progress(0.5, "halfway")
    fail_times = int(ctx.payload.get("fail_times", 0))
    marker = ctx.payload.get("marker")
    if marker and fail_times:
        from pathlib import Path

        p = Path(marker)
        n = int(p.read_text()) if p.exists() else 0
        if n < fail_times:
            p.write_text(str(n + 1))
            raise RuntimeError(f"planned failure {n + 1}")
    if ctx.payload.get("sleep"):
        import time

        for _ in range(int(ctx.payload["sleep"] * 10)):
            time.sleep(0.1)
            ctx.check_cancelled()
    return {"ok": True, "echo": ctx.payload.get("echo")}


@task("analyze_source")
def analyze_source(ctx: JobContext) -> dict[str, Any]:
    from . import analysis

    return analysis.analyze_source(int(ctx.payload["source_id"]), progress=ctx.progress,
                                   force=bool(ctx.payload.get("force")))


@task("find_candidates")
def find_candidates(ctx: JobContext) -> dict[str, Any]:
    from . import candidates

    out = candidates.find_candidates(int(ctx.payload["source_id"]), ctx.payload.get("campaign"),
                                     int(ctx.payload.get("max_candidates", 40)), progress=ctx.progress)
    return {"candidates": [c.id for c in out]}


@task("rank_candidates")
def rank_candidates(ctx: JobContext) -> dict[str, Any]:
    from . import candidates

    out = candidates.rank(source_id=ctx.payload.get("source_id"), campaign=ctx.payload.get("campaign"),
                          use_model=bool(ctx.payload.get("use_model", True)), progress=ctx.progress)
    return {"ranked": len(out)}


@task("render_candidate")
def render_candidate(ctx: JobContext) -> dict[str, Any]:
    from . import render

    clip = render.render_candidate(int(ctx.payload["candidate_id"]), ctx.payload.get("spec"), progress=ctx.progress)
    return {"clip_id": clip.id, "version_id": clip.current_version_id}


@task("rerender_clip")
def rerender_clip(ctx: JobContext) -> dict[str, Any]:
    from . import render

    clip = render.rerender(int(ctx.payload["clip_id"]), ctx.payload.get("changes") or {}, progress=ctx.progress)
    return {"clip_id": clip.id, "version_id": clip.current_version_id}


@task("render_top")
def render_top(ctx: JobContext) -> dict[str, Any]:
    from . import render

    clips = render.render_top(ctx.payload.get("campaign"), ctx.payload.get("source_id"),
                              int(ctx.payload.get("top", 5)), ctx.payload.get("spec"), progress=ctx.progress)
    return {"clips": [c.id for c in clips]}


@task("platform_variant")
def platform_variant(ctx: JobContext) -> dict[str, Any]:
    from . import render

    clip = render.create_variant(int(ctx.payload["clip_id"]), ctx.payload["platform"], progress=ctx.progress)
    return {"clip_id": clip.id, "version_id": clip.current_version_id}


@task("attach_broll")
def attach_broll(ctx: JobContext) -> dict[str, Any]:
    from . import broll

    clip = broll.attach(int(ctx.payload["clip_id"]), ctx.payload.get("provider", "textcard"),
                        int(ctx.payload.get("max_inserts", 2)))
    return {"clip_id": clip.id, "version_id": clip.current_version_id}


@task("publish_post")
def publish_post(ctx: JobContext) -> dict[str, Any]:
    from . import publishing

    return publishing.run_publish(int(ctx.payload["post_id"]))


@task("refresh_post_status")
def refresh_post_status(ctx: JobContext) -> dict[str, Any]:
    from . import publishing

    return {"updated": publishing.refresh_statuses()}


@task("sync_metrics")
def sync_metrics(ctx: JobContext) -> dict[str, Any]:
    from . import metrics

    return metrics.sync(ctx.payload.get("campaign_id"))


@task("run_campaign")
def run_campaign(ctx: JobContext) -> dict[str, Any]:
    from . import runner

    return runner.run_campaign(ctx.payload["campaign"], int(ctx.payload.get("render_top", 5)),
                               int(ctx.payload.get("max_candidates", 40)), bool(ctx.payload.get("autonomous")),
                               ctx.payload.get("spec"), progress=ctx.progress)


@task("live_session")
def live_session(ctx: JobContext) -> dict[str, Any]:
    from . import live

    src = live.make_source(ctx.payload["kind"], ctx.payload["target"], float(ctx.payload.get("speed", 1.0)))

    def stopped() -> bool:
        try:
            ctx.check_cancelled()
            return False
        except Exception:
            return True

    return live.run_session(ctx.payload["campaign"], src, int(ctx.payload.get("chunk_seconds", 30)),
                            int(ctx.payload.get("window_seconds", 120)), ctx.payload.get("max_seconds"),
                            int(ctx.payload.get("render_top", 0)), progress=ctx.progress, should_stop=stopped)
