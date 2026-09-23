"""`clipper` command line. Every command works without an agent except the
ones that need judgment (run, copy, insights --explain), which hand off to
Claude Code or Codex over MCP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import campaigns, db, revenue
from .analytics import insights, metrics
from .clipping import clips, sources
from .config import ensure_dirs, settings
from .pipeline import fit

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Local clipping operator: campaign footage in, approved clips out.")
campaign_app = typer.Typer(no_args_is_help=True, help="Create and inspect campaigns.")
source_app = typer.Typer(no_args_is_help=True, help="Long-form footage for a campaign.")
metrics_app = typer.Typer(no_args_is_help=True, help="Views and payouts for published posts.")
app.add_typer(campaign_app, name="campaign")
app.add_typer(source_app, name="source")
app.add_typer(metrics_app, name="metrics")
console = Console()


def _fail(msg: str) -> None:
    console.print(f"[red]error:[/] {msg}")
    raise typer.Exit(1)


@app.command()
def init() -> None:
    """Create the data directory and database."""
    s = settings()
    ensure_dirs(s)
    with db.connect():
        pass
    console.print(f"data dir: {s.home}")


# --- campaigns ---------------------------------------------------------------

@campaign_app.command("create")
def campaign_create(path: Path) -> None:
    """Create or update a campaign from a YAML file."""
    spec = campaigns.parse_yaml(path.read_text())
    camp = campaigns.create(spec, base_dir=path.parent.resolve())
    console.print(f"campaign [bold]{camp['slug']}[/] (id {camp['id']}): {spec.name}, CPM ${spec.rate.cpm:.2f}")
    registered = sources.list_for(camp["slug"])
    for s in registered:
        console.print(f"  source {s['id']}: {s['path']}")
    if len(spec.source) > len(registered):
        console.print(f"[yellow]{len(spec.source) - len(registered)} listed source(s) not found; "
                      f"add them with `clipper source add {camp['slug']} <path>`[/]")


@campaign_app.command("list")
def campaign_list() -> None:
    t = Table("slug", "name", "CPM", "min views", "max payout", "sources", "clips")
    for c in campaigns.list_all():
        t.add_row(c["slug"], c["name"], f"${c['cpm']:.2f}", f"{c['min_views']:,}",
                  "-" if c["max_payout"] is None else f"${c['max_payout']:,.0f}",
                  str(c["n_sources"]), str(c["n_clips"]))
    console.print(t)


@campaign_app.command("show")
def campaign_show(campaign: str) -> None:
    console.print_json(campaigns.spec(campaign).model_dump_json())


# --- sources -----------------------------------------------------------------

@source_app.command("add")
def source_add(campaign: str, path_or_url: str, title: Optional[str] = None) -> None:
    s = sources.add(campaign, path_or_url, title)
    console.print(f"source {s['id']}: {s['path']} ({s['duration'] / 60:.1f} min)")


@source_app.command("list")
def source_list(campaign: str) -> None:
    t = Table("id", "title", "minutes", "status", "clips")
    for s in sources.list_for(campaign):
        t.add_row(str(s["id"]), s["title"] or s["path"], f"{(s['duration'] or 0) / 60:.1f}", s["status"], str(s["n_clips"]))
    console.print(t)


@app.command()
def transcribe(source_id: int) -> None:
    """Transcribe one source locally with faster-whisper."""
    from .clipping.transcribe import transcribe as run_transcribe

    t = run_transcribe(source_id, progress=lambda f: console.print(f"\r  {f:5.0%}", end=""))
    console.print(f"\n{len(t.segments)} segments, language {t.language}")


# --- the loop ----------------------------------------------------------------

@app.command()
def run(
    campaign: str,
    candidates: int = typer.Option(20, help="Candidate moments the agent proposes per source"),
    render: int = typer.Option(5, help="How many top-scored clips to render"),
    platforms: Optional[str] = typer.Option(None, help="Comma list; default: the campaign's platforms"),
    agent: Optional[str] = typer.Option(None, help="claude (default) or codex"),
    model: Optional[str] = typer.Option(None, help="Model alias passed to the agent CLI"),
    openshorts: bool = typer.Option(False, help="Also run the source through a self-hosted OpenShorts"),
    framing: Optional[str] = typer.Option(None, help="crop | blur | openshorts (default: the agent's pick)"),
    no_publish: bool = typer.Option(False, "--no-publish", help="Stop after approval and copy"),
) -> None:
    """Footage → candidates → scores → renders → your approval → copy → publish."""
    from .pipeline import RunOptions, run as run_pipeline

    try:
        run_pipeline(campaign, RunOptions(
            candidates=candidates, render=render, agent=agent, model=model, openshorts=openshorts,
            platforms=[p.strip() for p in platforms.split(",")] if platforms else None,
            framing=framing, publish=not no_publish), console=console)
    except (LookupError, RuntimeError) as e:
        _fail(str(e))


@app.command("clips")
def clips_list(campaign: str, status: Optional[str] = None, limit: int = 30) -> None:
    """Clips ranked by AI score."""
    t = Table("id", "score", "status", "dur", "title", "opens with")
    for c in clips.list_clips(campaign, status, limit):
        t.add_row(str(c["id"]), "-" if c["ai_score"] is None else f"{c['ai_score']:.0f}", c["status"],
                  f"{c['end_time'] - c['start_time']:.0f}s", c["title"] or "", (c["opening_words"] or "")[:40])
    console.print(t)


@app.command("render")
def render_cmd(clip_ids: list[int], framing: Optional[str] = None,
               crop_x: Optional[float] = typer.Option(None, help="Pin the crop 0=left .. 1=right (default: follow the speaker)")) -> None:
    """Render specific clips (re-render with different framing, for example)."""
    for cid in clip_ids:
        c = clips.render_clip(cid, framing, crop_x)
        console.print(f"clip {cid}: {c['video_path']}")


@app.command()
def review(campaign: str) -> None:
    """Approve or reject rendered clips without running the full loop."""
    from .pipeline import parse_selection

    pending = clips.list_clips(campaign, "rendered")
    if not pending:
        console.print("nothing waiting for review")
        return
    for i, c in enumerate(pending, 1):
        console.print(f"{i}. [{c['ai_score']:.0f}] {c['title']}  [dim]{c['video_path']}[/]")
    idx = parse_selection(console.input("Approve clips? "), len(pending))
    rest = [c["id"] for i, c in enumerate(pending) if i not in idx]
    if idx:
        clips.set_review([pending[i]["id"] for i in idx], "approved")
    if rest:
        clips.set_review(rest, "rejected")
    console.print(f"approved {len(idx)}, rejected {len(rest)}")


@app.command()
def copy(campaign: str, agent: Optional[str] = None, platforms: Optional[str] = None) -> None:
    """Have the agent write platform copy for approved clips."""
    from .agents import prompts, runner

    spec = campaigns.spec(campaign)
    ids = [c["id"] for c in clips.list_clips(campaign, "approved")]
    if not ids:
        _fail("no approved clips")
    plats = [p.strip() for p in platforms.split(",")] if platforms else spec.platforms
    with console.status("agent writing copy…") as st:
        out = runner.run(prompts.WRITE_COPY.format(campaign=campaign, clip_ids=ids, platforms=plats),
                         agent, on_event=lambda m: st.update(fit(console, m)))
    console.print(out)


@app.command()
def publish(campaign: str, platforms: Optional[str] = None, clip: list[int] = typer.Option(None),
            schedule: Optional[str] = typer.Option(None, help="ISO datetime"),
            timezone: Optional[str] = None, dry_run: bool = False, yes: bool = False) -> None:
    """Post approved clips via Upload-Post (asks for confirmation)."""
    from .publishing import publisher

    spec = campaigns.spec(campaign)
    plats = [p.strip() for p in platforms.split(",")] if platforms else spec.platforms
    ids = clip or [c["id"] for c in clips.list_clips(campaign, "approved")]
    if not ids:
        _fail("no approved clips")
    for cid in ids:
        pre = publisher.publish(cid, plats, dry_run=True)
        if pre.get("problems"):
            console.print(f"[red]clip {cid} blocked:[/] {'; '.join(pre['problems'])}")
            continue
        console.print(f"clip {cid} → {', '.join(plats)}: {Path(pre['video']).name}")
        if dry_run:
            continue
        if not yes and console.input("Publish? [y/N] ").strip().lower() not in ("y", "yes"):
            continue
        res = publisher.publish(cid, plats, confirm=True, scheduled_date=schedule, timezone=timezone)
        for p in res.get("posts", []):
            console.print(f"  {'✗' if p['status'] == 'failed' else '✓'} {p['platform']} {p['status']} {p.get('url') or ''}")


@app.command()
def posts(campaign: Optional[str] = None) -> None:
    """Published posts and their latest numbers."""
    t = Table("post", "clip", "platform", "status", "views", "likes", "url")
    for p in revenue.posts_with_latest(campaign):
        t.add_row(str(p["id"]), str(p["clip_id"]), p["platform"], p["status"], f"{p['views']:,}",
                  str(p["likes"] or "-"), p["post_url"] or "")
    console.print(t)


@metrics_app.command("sync")
def metrics_sync() -> None:
    """Resolve pending uploads and pull analytics from Upload-Post."""
    from .publishing import publisher

    console.print(publisher.sync_metrics())


@metrics_app.command("add")
def metrics_add(post_id: int, views: int, likes: Optional[int] = None, comments: Optional[int] = None,
                shares: Optional[int] = None, saves: Optional[int] = None,
                payout: Optional[float] = typer.Option(None, help="What the campaign actually paid")) -> None:
    """Record a snapshot by hand (e.g. from the Content Rewards dashboard)."""
    metrics.record(post_id, views, likes, comments, shares, saves)
    if payout is not None:
        metrics.set_payout(post_id, payout)
    console.print("recorded")


@app.command()
def report(campaign: str) -> None:
    """Views, qualified views and revenue for a campaign."""
    r = revenue.report(campaign)
    t = Table("post", "clip", "platform", "views", "qualified", "estimated", "actual")
    for p in r["posts"]:
        t.add_row(str(p["id"]), p["clip_title"] or "", p["platform"], f"{p['views']:,}", f"{p['qualified_views']:,}",
                  f"${p['estimated']:,.2f}", "-" if p["actual_payout"] is None else f"${p['actual_payout']:,.2f}")
    console.print(t)
    console.print(f"CPM ${r['cpm']:.2f} · min views {r['minimum_views']:,} · views {r['total_views']:,} · "
                  f"qualified {r['qualified_views']:,}")
    console.print(f"[bold]estimated ${r['estimated_revenue']:,.2f}[/]"
                  + (" (budget cap hit)" if r["budget_capped"] else "")
                  + ("" if r["actual_revenue"] is None else f" · actual ${r['actual_revenue']:,.2f}"))


@app.command("insights")
def insights_cmd(campaign: Optional[str] = None, min_n: int = 3,
                 explain: bool = typer.Option(False, help="Have the agent interpret and save learnings"),
                 agent: Optional[str] = None) -> None:
    """What past clips teach us (feeds back into the judge)."""
    if explain:
        from .agents import prompts, runner

        clause = f', campaign="{campaign}"' if campaign else ""
        with console.status("agent analysing…") as st:
            out = runner.run(prompts.ANALYZE.format(min_n=min_n, campaign_clause=clause), agent,
                             on_event=lambda m: st.update(fit(console, m)))
        console.print(out)
        return
    a = insights.analyze(campaign, min_n)
    if not a["n_posts"]:
        console.print(a["note"])
        return
    console.print(f"{a['n_posts']} posts, average {a['overall_avg_views']:,} views")
    t = Table("feature", "value", "n", "avg views", "vs avg")
    for r in a["patterns"][:20]:
        t.add_row(r["feature"], r["value"], str(r["n"]), f"{r['avg_views']:,}", f"{r['lift_pct']:+.0f}%")
    console.print(t)
    console.print(f"judge calibration: {a['calibration']}")


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (what Claude Code / Codex launch)."""
    from .mcp_server import main as serve

    serve()


@app.command("mcp-config")
def mcp_config(codex: bool = typer.Option(False, help="Print a Codex config.toml block instead")) -> None:
    """Print MCP config pointing at this install and data dir."""
    from .agents.runner import mcp_server_spec

    spec = mcp_server_spec()
    if codex:
        env = ", ".join(f'{k} = "{v}"' for k, v in spec["env"].items())
        print(f'[mcp_servers.clipper]\ncommand = "{spec["command"]}"\nargs = {json.dumps(spec["args"])}\n'
              f"env = {{ {env} }}")
    else:
        print(json.dumps({"mcpServers": {"clipper": spec}}, indent=2))


def main() -> None:
    app()
