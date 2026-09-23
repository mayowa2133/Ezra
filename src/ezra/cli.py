"""`ezra` command line. Same application services as the API and MCP server.

Long operations become jobs (progress, logs, retries, cancellation) and, by
default, run right here in a child process while a progress bar polls the job;
pass --queue to leave them for a running worker instead."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import analytics as perf
from . import (
    audit,
    brandkits,
    campaigns,
    candidates,
    costs,
    db,
    economics,
    experiments,
    jobs,
    metadata,
    metrics,
    publishing,
    render,
    review,
    runner,
    secrets,
    sources,
    transcription,
)
from .config import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False,
                  help="Ezra: agent-native short-form clipping and performance optimization.")
campaign_app = typer.Typer(no_args_is_help=True, help="Create, import and inspect campaigns.")
source_app = typer.Typer(no_args_is_help=True, help="Authorized long-form footage.")
clip_app = typer.Typer(no_args_is_help=True, help="Rendered clips.")
accounts_app = typer.Typer(no_args_is_help=True, help="Publishing accounts and OAuth connections.")
secrets_app = typer.Typer(no_args_is_help=True, help="Encrypted secret store (names only are ever printed).")
metrics_app = typer.Typer(no_args_is_help=True, help="Post metrics.")
exp_app = typer.Typer(no_args_is_help=True, help="A/B experiments.")
brand_app = typer.Typer(no_args_is_help=True, help="Brand kits.")
jobs_app = typer.Typer(no_args_is_help=True, help="Background jobs.")
live_app = typer.Typer(no_args_is_help=True, help="Live clipping.")
db_app = typer.Typer(no_args_is_help=True, help="Database.")
for name, sub in (("campaign", campaign_app), ("source", source_app), ("clip", clip_app),
                  ("accounts", accounts_app), ("secrets", secrets_app), ("metrics", metrics_app),
                  ("experiment", exp_app), ("brandkit", brand_app), ("jobs", jobs_app), ("live", live_app),
                  ("db", db_app)):
    app.add_typer(sub, name=name)
console = Console()


def _clip_text(text: str, n: int) -> str:
    """At most n characters, cut on a word boundary."""
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def fail(msg: str) -> None:
    console.print(f"[red]error:[/] {msg}")
    raise typer.Exit(1)


def run_job(kind: str, payload: dict[str, Any], queue: bool = False, label: str | None = None,
            dedupe_key: str | None = None) -> dict[str, Any]:
    """Enqueue; unless --queue, execute it here (isolated child process) with a live progress bar."""
    from . import worker

    job = jobs.enqueue(kind, payload, dedupe_key=dedupe_key)
    if queue:
        console.print(f"queued job {job.id} ({kind}); follow with `ezra jobs show {job.id}`")
        return jobs.as_dict(job)
    claimed = jobs.claim(job.id)
    done: dict[str, Any] = {}
    if claimed is not None:
        t = threading.Thread(target=lambda: done.setdefault("job", worker.run_isolated(claimed)
                                                            if get_settings().job_isolation else jobs.run(claimed)))
        t.start()
    with Progress(TextColumn(f"[bold]{label or kind}[/]"), BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
                  TextColumn("[dim]{task.description}"), TimeElapsedColumn(), console=console, transient=True) as bar:
        task = bar.add_task("", total=1.0)
        while True:
            j = jobs.get(job.id)
            bar.update(task, completed=j.progress, description=(j.message or "")[:60])
            if j.status in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.4)
    j = jobs.get(job.id)
    if j.status != "completed":
        err = (j.error or {}).get("message") or j.message
        fail(f"job {j.id} {j.status}: {err}\n  details: `ezra jobs show {j.id}`  retry: `ezra jobs retry {j.id}`")
    return j.result or {}


# --- setup ---------------------------------------------------------------------------------

@app.command()
def init() -> None:
    """Create the data directory and run migrations."""
    s = get_settings()
    db.migrate()
    console.print(f"EZRA_HOME  {s.home}\ndatabase   {s.db_url.split('://')[0]}\nstorage    {s.storage}")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply migrations up to head (SQLite or PostgreSQL, per EZRA_DATABASE_URL)."""
    db.migrate()
    console.print("migrations applied")


@db_app.command("revision")
def db_revision(message: str = typer.Option(..., "-m", "--message")) -> None:
    """Autogenerate a migration from model changes (review the file before committing)."""
    console.print(f"wrote {db.revision(message)}")


# --- campaigns -------------------------------------------------------------------------------

@campaign_app.command("import")
def campaign_import(path: Path,
                    no_sources: bool = typer.Option(False, help="Don't ingest listed source files")) -> None:
    """Import campaigns from YAML, JSON or CSV."""
    for c in campaigns.import_file(path, ingest_sources=not no_sources):
        console.print(f"campaign [bold]{c.slug}[/] ({c.name}): CPM {c.currency} {c.cpm:.2f}, "
                      f"{len(c.rules)} rules, platforms {', '.join(c.allowed_platforms)}")
        for s in sources.list_sources(c.slug):
            console.print(f"  source {s.id}: {s.title} ({s.duration / 60:.1f} min, rights {s.rights_status})")


@campaign_app.command("create")
def campaign_create(name: str = typer.Option(..., prompt=True), cpm: float = typer.Option(..., prompt="CPM ($)"),
                    min_views: int = typer.Option(0, prompt="Minimum qualified views"),
                    max_payout: float | None = typer.Option(None, help="Max payout per clip"),
                    hashtags: str = typer.Option("", prompt="Required hashtags (space separated)"),
                    platforms: str = typer.Option("tiktok,instagram,youtube", prompt="Platforms"),
                    min_duration: float = 15, max_duration: float = 60, brief: str = "") -> None:
    """Create a campaign interactively."""
    spec = campaigns.CampaignSpec(name=name, cpm=cpm, minimum_views=min_views, maximum_payout=max_payout,
                                  hashtags=hashtags.split(), platforms=[p.strip() for p in platforms.split(",")],
                                  min_duration=min_duration, max_duration=max_duration, brief=brief or None)
    c = campaigns.upsert(spec)
    console.print(f"campaign [bold]{c.slug}[/] created with {len(c.rules)} rules")


@campaign_app.command("list")
def campaign_list() -> None:
    t = Table("slug", "name", "CPM", "min views", "max/clip", "platforms", "rules")
    for c in campaigns.list_campaigns():
        t.add_row(c.slug, c.name, f"{c.cpm:.2f}", f"{c.min_qualified_views:,}",
                  "-" if c.max_payout_per_clip is None else f"{c.max_payout_per_clip:,.0f}",
                  ",".join(c.allowed_platforms), str(len(c.rules)))
    console.print(t)


@campaign_app.command("show")
def campaign_show(ref: str) -> None:
    console.print_json(json.dumps(campaigns.to_dict(campaigns.get(ref)), default=str))


# --- sources & analysis ------------------------------------------------------------------------

@source_app.command("add")
def source_add(path: str, campaign: str | None = typer.Option(None, "--campaign", "-c"),
               rights: str = typer.Option("unknown",
                                          help="campaign_supplied | owned | licensed | permission | unknown"),
               title: str | None = None) -> None:
    """Ingest a local file (or URL with yt-dlp) you are authorized to use."""
    s = sources.ingest(path, campaign, title, rights, actor="cli")
    console.print(f"source {s.id}: {s.title} ({s.duration / 60:.1f} min, {s.width}x{s.height}) "
                  f"rights {s.rights_status}")


@source_app.command("list")
def source_list(campaign: str | None = typer.Option(None, "--campaign", "-c")) -> None:
    t = Table("id", "title", "min", "status", "rights", "candidates")
    for s in sources.list_sources(campaign):
        d = sources.to_dict(s)
        t.add_row(str(s.id), s.title, f"{s.duration / 60:.1f}", s.status, s.rights_status, str(d["n_candidates"]))
    console.print(t)


@source_app.command("rights")
def source_rights(source_id: int, status: str, basis: str | None = None, notes: str | None = None) -> None:
    s = sources.set_rights(source_id, status, basis, notes, actor="cli")
    console.print(f"source {s.id} rights: {s.rights_status} ({s.rights_basis})")


def _sources_for(source_id: int | None, campaign: str | None) -> list[int]:
    if source_id is not None:
        return [source_id]
    ids = [s.id for s in sources.list_sources(campaign)]
    if not ids:
        fail("no sources; `ezra source add <file> --campaign <slug>`")
    return ids


@app.command()
def analyze(source_id: int | None = typer.Option(None, "--source", "-s"),
            campaign: str | None = typer.Option(None, "--campaign", "-c"), force: bool = False,
            queue: bool = False) -> None:
    """Transcribe, diarize and analyze scenes, faces, silence and topics."""
    for sid in _sources_for(source_id, campaign):
        out = run_job("analyze_source", {"source_id": sid, "force": force}, queue, f"analyze {sid}",
                      dedupe_key=f"analyze-{sid}")
        if not queue:
            p = out.get("products", {})
            console.print(f"source {sid}: {p.get('speakers', {}).get('n_speakers', '?')} speakers, "
                          f"{p.get('scenes', {}).get('scenes', '?')} scenes, face rate "
                          f"{p.get('faces', {}).get('detection_rate', '-')}, topics: "
                          f"{'; '.join(p.get('topics', {}).get('topics', [])[:5])}")


@app.command()
def transcript(source_id: int, start: float = 0.0) -> None:
    """Print the timestamped, speaker-labelled transcript."""
    console.print(transcription.page(source_id, start, 200000)["text"])


# --- candidates & ranking ----------------------------------------------------------------------

@app.command("find")
def find(source_id: int | None = typer.Option(None, "--source", "-s"),
         campaign: str | None = typer.Option(None, "--campaign", "-c"), max_candidates: int = 40,
         queue: bool = False) -> None:
    """Generate and rank candidate moments."""
    for sid in _sources_for(source_id, campaign):
        out = run_job("find_candidates", {"source_id": sid, "campaign": campaign, "max_candidates": max_candidates},
                      queue, f"candidates {sid}")
        if not queue:
            console.print(f"source {sid}: {len(out['candidates'])} candidates")


@app.command("candidates")
def candidates_cmd(campaign: str | None = typer.Option(None, "--campaign", "-c"),
                   source_id: int | None = typer.Option(None, "--source", "-s"), top: int = 20,
                   all: bool = typer.Option(False, "--all", help="Include compliance failures")) -> None:
    """Ranked candidates (generated on first use; `ezra find` to regenerate)."""
    items = candidates.list_candidates(campaign, source_id, top, include_failed=all)
    if not items:  # nothing found yet for this scope: generate first
        for sid in _sources_for(source_id, campaign):
            if not candidates.list_candidates(source_id=sid, include_failed=True):
                run_job("find_candidates", {"source_id": sid, "campaign": campaign}, label=f"candidates {sid}")
        items = candidates.list_candidates(campaign, source_id, top, include_failed=all)
    t = Table()
    for name, width in (("id", 3), ("rank", 5), ("dur", 4), ("hook", 8), ("compliance", 10), ("EV/post", 7),
                        ("p(qual)", 7)):
        t.add_column(name, min_width=width, no_wrap=True)
    t.add_column("title", max_width=38, no_wrap=True, overflow="ellipsis")
    for c in items:
        ev = c.expected_value or {}
        t.add_row(str(c.id), f"{c.rank_score:.1f}" if c.rank_score is not None else "-", f"{c.end - c.start:.0f}s",
                  c.hook_type or "-", {"PASS": "[green]PASS[/]", "FAIL": "[red]FAIL[/]",
                                      "REVIEW_REQUIRED": "[yellow]REVIEW[/]"}.get(c.compliance_status or "", "-"),
                  f"{ev.get('ev_per_post', '-')}", f"{ev.get('p_qualify', '-')}", (c.title or "")[:48])
    console.print(t)


@app.command()
def candidate(candidate_id: int) -> None:
    """Full detail for one candidate: transcript, factor scores, explanations, compliance, EV."""
    console.print_json(json.dumps(candidates.to_dict(candidates.get(candidate_id), detail=True), default=str))


@app.command()
def rank(campaign: str | None = typer.Option(None, "--campaign", "-c"), no_model: bool = False,
         queue: bool = False) -> None:
    """Re-rank candidates (model critique when EZRA_LLM is configured)."""
    out = run_job("rank_candidates", {"campaign": campaign, "use_model": not no_model}, queue, "rank")
    if not queue:
        console.print(f"ranked {out['ranked']} candidates")


# --- rendering ----------------------------------------------------------------------------------

def _spec(aspect: str | None, theme: str | None, layout: str | None, punch_in: str | None,
          no_silence: bool, no_fillers: bool) -> dict[str, Any]:
    s: dict[str, Any] = {}
    if aspect:
        s["aspect"] = aspect
    if theme:
        s["caption_theme"] = theme
    if layout:
        s["layout"] = layout
    if punch_in:
        s["punch_in"] = punch_in
    if no_silence:
        s["remove_silence"] = False
    if no_fillers:
        s["remove_fillers"] = False
    return s


@app.command("render")
def render_cmd(top: int = typer.Option(5, help="Render the N best unrendered candidates"),
               candidate_id: int | None = typer.Option(None, "--candidate"),
               campaign: str | None = typer.Option(None, "--campaign", "-c"),
               aspect: str | None = None, theme: str | None = None, layout: str | None = None,
               punch_in: str | None = None, no_silence_removal: bool = False, no_filler_removal: bool = False,
               queue: bool = False) -> None:
    """Render clips (9:16 by default) with tracking, captions and edits."""
    spec = _spec(aspect, theme, layout, punch_in, no_silence_removal, no_filler_removal) or None
    if candidate_id:
        out = run_job("render_candidate", {"candidate_id": candidate_id, "spec": spec}, queue, f"render {candidate_id}")
        ids = [out["clip_id"]] if not queue and out.get("clip_id") else []
    else:
        out = run_job("render_top", {"campaign": campaign, "top": top, "spec": spec}, queue, f"render top {top}")
        ids = out.get("clips", []) if not queue else []
    for cid in ids:
        c = render.get_clip(int(cid))
        v = render.current_version(c)
        if v is None:
            continue
        key = v.video_key or ""
        where = get_settings().storage_dir / key if get_settings().storage == "local" else key
        console.print(f"clip {cid}: {v.duration:.1f}s {v.width}x{v.height} layout {v.layout_used}  [dim]{where}[/]")


# --- review ---------------------------------------------------------------------------------------

@app.command("review")
def review_cmd(campaign: str | None = typer.Option(None, "--campaign", "-c"),
               open_video: bool = typer.Option(False, "--open", help="Open each clip in the default player")) -> None:
    """Keyboard review: a = approve, r = reject, s = skip, q = quit."""
    queue_ = review.queue(campaign)
    if not queue_:
        console.print("nothing waiting for review")
        return
    for i, card in enumerate(queue_, 1):
        cand = card["candidate"]
        path = get_settings().storage_dir / card["video_key"] if card["video_key"] else None
        console.rule(f"[{i}/{len(queue_)}] clip {card['clip_id']}: {card['title']}")
        console.print(f"rank {cand['rank_score']}  ({cand['scorer']}, confidence {cand['confidence']})  "
                      f"{card['duration']:.1f}s  layout {card['layout']}")
        console.print("scores: " + "  ".join(f"{k} {v:.0f}" for k, v in cand["scores"].items() if v is not None))
        comp = card["compliance"]
        console.print(f"compliance: {comp['status']}" + "".join(f"\n  - {r['message']}" for r in comp["reasons"]))
        ev = card["expected_value"] or {}
        if ev:
            console.print(f"expected value: ${ev.get('ev_per_post')} per post (p10 ${ev.get('p10')} - p90 "
                          f"${ev.get('p90')}), p(qualify) {ev.get('p_qualify')}  [dim]{ev.get('basis')}[/]")
        console.print(f"[dim]{cand.get('transcript', '')[:400]}[/]")
        if path:
            console.print(f"video: {path}")
            if open_video:
                os.system(f"open '{path}' 2>/dev/null || xdg-open '{path}' 2>/dev/null")
        while True:
            key = console.input("a=approve r=reject s=skip q=quit > ").strip().lower()
            if key in ("a", "r", "s", "q"):
                break
        if key == "q":
            return
        if key == "a":
            review.approve(card["clip_id"], actor="cli")
            console.print("[green]approved[/]")
        elif key == "r":
            reason = console.input("reason (optional) > ").strip() or None
            review.reject(card["clip_id"], actor="cli", reason=reason)
            console.print("[red]rejected[/]")


@app.command()
def approve(clip_id: int, notes: str | None = None) -> None:
    """Approve a rendered clip for publishing (FAIL-compliance clips are refused)."""
    review.approve(clip_id, actor="cli", notes=notes)
    console.print(f"clip {clip_id} approved")


@app.command()
def reject(clip_id: int, reason: str | None = None) -> None:
    """Reject a clip, with an optional reason kept for learning."""
    review.reject(clip_id, actor="cli", reason=reason)
    console.print(f"clip {clip_id} rejected")


@clip_app.command("list")
def clip_list(campaign: str | None = typer.Option(None, "--campaign", "-c"), status: str | None = None) -> None:
    t = Table("id", "status", "v", "dur", "layout", "title")
    for c in render.list_clips(campaign, status):
        v = render.current_version(c)
        t.add_row(str(c.id), c.status, str(v.version if v else "-"), f"{v.duration:.0f}s" if v and v.duration else "-",
                  (v.layout_used if v else "-") or "-", (c.title or "")[:50])
    console.print(t)


@clip_app.command("show")
def clip_show(clip_id: int) -> None:
    console.print_json(json.dumps(render.clip_dict(render.get_clip(clip_id), detail=True), default=str))


@clip_app.command("metadata")
def clip_metadata(clip_id: int, platforms: str | None = None, no_model: bool = False) -> None:
    """Generate per-platform titles, captions and hashtags."""
    out = metadata.generate(clip_id, platforms.split(",") if platforms else None, use_model=not no_model)
    for p, m in out["metadata"].items():
        console.print(f"[bold]{p}[/] ({out['compliance'][p]['status']}) {m.get('title') or ''}\n{m['caption']}\n")


@clip_app.command("rerender")
def clip_rerender(clip_id: int, aspect: str | None = None, theme: str | None = None,
                  layout: str | None = None, punch_in: str | None = None, queue: bool = False) -> None:
    run_job("rerender_clip", {"clip_id": clip_id, "changes": _spec(aspect, theme, layout, punch_in, False, False)},
            queue, f"rerender {clip_id}")
    console.print(f"clip {clip_id} re-rendered")


@clip_app.command("variant")
def clip_variant(clip_id: int, platform: str, queue: bool = False) -> None:
    """Platform variant (e.g. linkedin 1:1, x 16:9)."""
    run_job("platform_variant", {"clip_id": clip_id, "platform": platform}, queue, f"{platform} variant")
    console.print(f"clip {clip_id}: {platform} variant rendered")


@clip_app.command("broll")
def clip_broll(clip_id: int, provider: str = "textcard", max_inserts: int = 2, queue: bool = False) -> None:
    """Add rights-cleared B-roll (library | pexels | textcard)."""
    run_job("attach_broll", {"clip_id": clip_id, "provider": provider, "max_inserts": max_inserts}, queue, "b-roll")
    console.print(f"clip {clip_id} re-rendered with B-roll")


@clip_app.command("export")
def clip_export(clip_id: int, dest: Path = typer.Option(Path("exports"))) -> None:
    """Export an approved clip: mp4, thumbnail, SRT, ASS, metadata JSON."""
    files = render.export_clip(clip_id, dest / f"clip-{clip_id}")
    for k, v in files.items():
        console.print(f"{k:>4}  {v}")


# --- publishing ----------------------------------------------------------------------------------

@app.command()
def publish(clip: list[int] | None = typer.Option(None, "--clip"),
            campaign: str | None = typer.Option(None, "--campaign", "-c"),
            platforms: str | None = None, private: bool = False,
            schedule: str | None = typer.Option(None, help="ISO datetime, e.g. 2026-10-01T18:00"),
            tz: str | None = typer.Option(None, help="IANA timezone for --schedule"),
            yes: bool = False, dry_run: bool = False) -> None:
    """Publish approved clips (asks before posting)."""
    ids = clip or [c.id for c in render.list_clips(campaign, "approved")]
    if not ids:
        fail("no approved clips")
    plats = platforms.split(",") if platforms else None
    vis = "private" if private else "public"
    for cid in ids:
        pre = publishing.publish_clip(cid, plats, visibility=vis, schedule_at=schedule, tz=tz, confirm=False)
        if pre["problems"]:
            console.print(f"[red]clip {cid} blocked:[/]\n  " + "\n  ".join(pre["problems"]))
            continue
        for p in pre["posts"]:
            note = f"  [yellow]review: {'; '.join(p['review_notes'])}[/]" if p["review_notes"] else ""
            console.print(f"clip {cid} → {p['platform']} @{p['account']} via {p['provider']} ({vis})"
                          f"{' at ' + schedule if schedule else ''}{note}")
        if dry_run or (not yes and console.input("Publish? [y/N] ").strip().lower() not in ("y", "yes")):
            continue
        res = publishing.publish_clip(cid, plats, visibility=vis, schedule_at=schedule, tz=tz, confirm=True,
                                      actor="cli")
        for job_id in res.get("job_ids", []):
            claimed = jobs.claim(job_id)   # run now; a running worker may already have it
            if claimed is not None:
                jobs.run(claimed)
            else:
                jobs.wait(job_id, timeout=900)
        for pid in res.get("post_ids", []):
            p = publishing.get_post(pid)
            mark = "[green]✓[/]" if p.status in ("published", "scheduled") else ("[yellow]…[/]" if p.status ==
                                                                                 "publishing" else "[red]✗[/]")
            console.print(f"  {mark} post {pid} {p.platform} {p.status} {p.url or ''} {p.error or ''}")


@app.command()
def posts(campaign: str | None = typer.Option(None, "--campaign", "-c"), status: str | None = None) -> None:
    """Posts with status, visibility, latest views and URL."""
    t = Table("post", "clip", "platform", "provider", "status", "vis", "views", "url")
    for p in publishing.list_posts(campaign, status):
        last = (metrics.history(p.id) or [{}])[-1]
        t.add_row(str(p.id), str(p.clip_id), p.platform, p.provider, p.status, p.visibility,
                  f"{last.get('views') or 0:,}", (p.url or "")[:60])
    console.print(t)


@accounts_app.command("list")
def accounts_list() -> None:
    t = Table("id", "platform", "provider", "handle", "status", "credential")
    for a in publishing.list_accounts():
        d = publishing.account_dict(a)
        t.add_row(str(a.id), a.platform, a.provider, a.handle, a.status, "yes" if d["has_credential"] else "-")
    console.print(t)


@accounts_app.command("add")
def accounts_add(platform: str, provider: str, handle: str, credential_ref: str | None = None,
                 tz: str = "UTC", export_dir: Path | None = None,
                 audited: bool = typer.Option(False, help="TikTok: the app passed TikTok's audit, "
                                                          "so public posts are allowed")) -> None:
    """Register or update an account (local-export needs no credential; upload-post uses UPLOAD_POST_API_KEY)."""
    meta: dict[str, Any] = {"dir": str(export_dir.resolve())} if export_dir else {}
    if audited:
        meta["unaudited"] = False
    a = publishing.add_account(platform, provider, handle, credential_ref, tz, meta)
    console.print(f"account {a.id}: {a.platform} via {a.provider} @{a.handle}")


@accounts_app.command("connect")
def accounts_connect(provider: str) -> None:
    """Start OAuth for youtube | tiktok | instagram; open the printed URL."""
    out = publishing.connect_start(provider)
    console.print(f"Open this URL to authorize (redirects to {out['redirect_uri']}; the API must be running):\n"
                  f"{out['authorize_url']}")


@secrets_app.command("set")
def secrets_set(ref: str, value: str = typer.Option(..., prompt=True, hide_input=True)) -> None:
    """Store a secret (JSON or string) in the encrypted store."""
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    secrets.put(ref, parsed)
    audit.record("secret.stored", "secret", ref, actor="cli")   # the name only, never the value
    console.print(f"stored {ref}")


@secrets_app.command("list")
def secrets_list() -> None:
    for r in secrets.refs():
        console.print(r)


# --- metrics, earnings, analytics --------------------------------------------------------------------

@metrics_app.command("sync")
def metrics_sync(campaign: str | None = typer.Option(None, "--campaign", "-c")) -> None:
    """Pull metrics from each platform API for published posts."""
    cid = campaigns.get(campaign).id if campaign else None
    out = run_job("sync_metrics", {"campaign_id": cid}, label="sync metrics")
    console.print(f"{out['snapshots']} snapshots stored, {out['skipped']} skipped"
                  + "".join(f"\n  [red]post {e['post_id']}: {e['error']}[/]" for e in out["errors"]))


@metrics_app.command("add")
def metrics_add(post_id: int, views: int, likes: int | None = None, comments: int | None = None,
                shares: int | None = None, saves: int | None = None,
                payout: float | None = typer.Option(None, help="Confirmed payout for this post")) -> None:
    """Record a snapshot by hand (e.g. from a campaign dashboard)."""
    metrics.record(post_id, views=views, likes=likes, comments=comments, shares=shares, saves=saves)
    if payout is not None:
        economics.record_revenue(publishing.get_post(post_id).campaign_id, payout, post_id)  # type: ignore[arg-type]
    console.print("recorded")


@metrics_app.command("import")
def metrics_import(path: Path) -> None:
    """CSV with post_id or url plus views, likes, comments, shares, saves, ..."""
    console.print(metrics.import_csv(path.read_text()))


@app.command()
def report(campaign: str) -> None:
    """Campaign earnings, costs, margin and what is working."""
    e = economics.earnings(campaign)
    t = Table("post", "platform", "views", "qualified", "estimated", "confirmed", "metrics from")
    for p in e["posts"]:
        t.add_row(str(p["post_id"]), p["platform"], f"{p['views']:,}", f"{p['qualified_views']:,}",
                  f"{p['estimated']:,.2f}", "-" if p["confirmed"] is None else f"{p['confirmed']:,.2f}",
                  p["metrics_provider"] or "-")
    console.print(t)
    console.print(f"CPM {e['cpm']:.2f} · min views {e['min_qualified_views']:,} · views {e['views']:,} · qualified "
                  f"{e['qualified_views']:,} ({e['qualifying_posts']} posts)")
    console.print(f"[bold]estimated {e['currency']} {e['gross_estimated']:,.2f}[/]"
                  f"{' (budget cap hit)' if e['budget_capped'] else ''} · confirmed {e['confirmed']:,.2f} · "
                  f"processing cost ~{e['costs']['total_usd']:.2f} · margin ~{e['margin_estimate']:,.2f}")
    ins = perf.insights(campaigns.get(campaign).id)
    console.print("\n[bold]observations[/]")
    for o in ins["observations"]:
        console.print(f"  - {o}")


@app.command()
def earnings(campaign: str) -> None:
    """Qualified views, estimated and confirmed payout for a campaign (JSON)."""
    console.print_json(json.dumps(economics.earnings(campaign), default=str))


@app.command()
def insights(campaign: str | None = typer.Option(None, "--campaign", "-c")) -> None:
    """Cohorts, calibration and regression over published results."""
    cid = campaigns.get(campaign).id if campaign else None
    console.print_json(json.dumps(perf.insights(cid), default=str))


@app.command()
def optimize(campaign: str, apply: bool = False) -> None:
    """Suggest (or --apply) scoring weights learned from this campaign's results."""
    console.print_json(json.dumps(runner.optimize(campaign, apply), default=str))


@app.command()
def cost(campaign: str | None = typer.Option(None, "--campaign", "-c")) -> None:
    """Recorded processing costs (transcription, analysis, render, storage, model calls)."""
    console.print_json(json.dumps(costs.summary(campaigns.get(campaign).id if campaign else None)))


# --- experiments, brand kits -----------------------------------------------------------------------------

@exp_app.command("create")
def exp_create(campaign: str, name: str, factor: str, values: str = typer.Option(..., help="comma list"),
               hypothesis: str | None = None, min_samples: int = 5) -> None:
    vals: list[Any] = []
    for v in values.split(","):
        v = v.strip()
        vals.append(True if v == "true" else False if v == "false" else int(v) if v.isdigit() else v)
    e = experiments.create(campaign, name, factor, vals, hypothesis, min_samples)
    console.print(f"experiment {e.id}: {factor} {[x['key'] + '=' + str(x['value']) for x in e.variants]}")


@exp_app.command("assign")
def exp_assign(experiment_id: int, clip_id: int) -> None:
    console.print(experiments.assign(experiment_id, clip_id))


@exp_app.command("analyze")
def exp_analyze(experiment_id: int) -> None:
    console.print_json(json.dumps(experiments.analyze(experiment_id)))


@brand_app.command("create")
def brand_create(path: Path) -> None:
    """Create/update a brand kit from YAML (name, logo, colors, caption_theme, ...)."""
    import yaml

    k = brandkits.upsert(brandkits.BrandKitSpec.model_validate(yaml.safe_load(path.read_text())))
    console.print(f"brand kit {k.id}: {k.name}")


@brand_app.command("list")
def brand_list() -> None:
    for k in brandkits.list_kits():
        console.print(f"{k.id}: {k.name} theme={k.caption_theme} logo={'yes' if k.logo_key else 'no'}")


# --- jobs ---------------------------------------------------------------------------------------------------

@jobs_app.command("list")
def jobs_list(status: str | None = None, limit: int = 30) -> None:
    t = Table("id", "kind", "status", "progress", "attempts", "message")
    for j in jobs.list_jobs(status, limit=limit):
        t.add_row(str(j.id), j.kind, j.status, f"{j.progress:.0%}", f"{j.attempts}/{j.max_attempts}",
                  (j.message or "")[:60])
    console.print(t)


@jobs_app.command("show")
def jobs_show(job_id: int) -> None:
    console.print_json(json.dumps(jobs.as_dict(jobs.get(job_id), with_logs=True), default=str))


@jobs_app.command("cancel")
def jobs_cancel(job_id: int) -> None:
    console.print(f"job {job_id}: {jobs.cancel(job_id).status}")


@jobs_app.command("retry")
def jobs_retry(job_id: int) -> None:
    console.print(f"job {job_id}: {jobs.retry(job_id).status}")


# --- the loop, live, servers ------------------------------------------------------------------------------------

@app.command()
def run(campaign: str, top: int = 5, max_candidates: int = 40,
        autonomous: bool = typer.Option(False, help="Auto-approve PASS clips and publish (opt-in, see docs)"),
        queue: bool = False) -> None:
    """analyze → candidates → rank → render top N → review queue (→ publish with --autonomous)."""
    out = run_job("run_campaign", {"campaign": campaign, "render_top": top, "max_candidates": max_candidates,
                                   "autonomous": autonomous}, queue, f"run {campaign}", dedupe_key=f"run-{campaign}")
    if queue:
        return
    console.print(f"{out['sources']} source(s) · {out['candidates']} candidates ({out['publishable']} publishable) · "
                  f"rendered {len(out['rendered'])} · review queue {out['review_queue']}")
    cards = [c for c in review.queue(campaign) if c["clip_id"] in set(out["rendered"])]
    if cards:
        t = Table("clip", "rank", "dur", "layout", "compliance", "EV/post", "opens with", title="ready for review")
        for c in cards:
            cand, ev = c["candidate"], c["expected_value"] or {}
            t.add_row(str(c["clip_id"]), f"{cand.get('rank_score') or 0:.1f}", f"{c['duration'] or 0:.0f}s",
                      str(c["layout"]), c["compliance"]["status"],
                      "-" if ev.get("ev_per_post") is None else f"${ev['ev_per_post']:,.2f}",
                      f"\"{_clip_text(cand.get('opens_with') or cand.get('transcript') or '', 60)}\"")
        console.print(t)
    if "autonomous" in out:
        console.print(f"autonomous: {out['autonomous']}")
    console.print("next: `ezra review` (or the dashboard /review)")


@live_app.command("start")
def live_start(campaign: str, file: Path | None = None, url: str | None = None,
               kind: str = typer.Option("file", help="file | hls | youtube"), speed: float = 1.0,
               chunk_seconds: int = 30, window_seconds: int = 120, max_seconds: float | None = None,
               render_top: int = 0, queue: bool = False) -> None:
    """Clip a live stream (or replay a file as one) into the review queue."""
    target = str(file.resolve()) if file else url
    if not target:
        fail("give --file or --url")
    out = run_job("live_session", {"campaign": campaign, "kind": "file" if file else kind, "target": target,
                                   "speed": speed, "chunk_seconds": chunk_seconds, "window_seconds": window_seconds,
                                   "max_seconds": max_seconds, "render_top": render_top}, queue, "live")
    if not queue:
        console.print(f"session {out['session']}: {out['windows']} windows, {out['seconds']:.0f}s, "
                      f"{len(out['candidates'])} candidates")


@app.command()
def worker() -> None:
    """Run the job worker."""
    from .worker import main as worker_main

    worker_main([])


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000,
          embedded_worker: bool = typer.Option(True, help="Also run a worker thread in this process")) -> None:
    """Run the API (and, by default, a worker) for local use."""
    import uvicorn

    if embedded_worker:
        os.environ["EZRA_EMBEDDED_WORKER"] = "1"
    uvicorn.run("ezra.api.app:app", host=host, port=port, log_level="info")


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (with an embedded worker)."""
    from .mcp_server import main as mcp_main

    mcp_main()


@app.command()
def audit_log(limit: int = 30) -> None:
    """Recent audit events: approvals, publishes, rights changes, secret writes."""
    for e in audit.recent(limit):
        console.print(f"{db.aware(e.at):%Y-%m-%d %H:%M} {e.actor:<10} {e.action:<22} {e.entity_type} {e.entity_id}")


@app.command()
def benchmark(out: Path = typer.Option(Path("benchmarks/results"), help="Where to write the report"),
              fixtures: str = typer.Option("solo,podcast,multi,scenes")) -> None:
    """Build fixtures and run the end-to-end quality benchmark."""
    from .benchmark.run import run_benchmark

    report_ = run_benchmark(out, tuple(fixtures.split(",")), console=console)
    console.print(f"report: {report_}")


def main() -> None:
    """Expected failures (unknown id, bad input, missing credential, refused action) print one
    line and exit 1; anything else keeps its traceback."""
    from .publishing.base import PublishError
    from .secrets import SecretError
    from .storage import StorageError
    from .worker import quiet_libraries

    quiet_libraries()
    try:
        rc = app(standalone_mode=False)
        if isinstance(rc, int) and rc:
            raise SystemExit(rc)
    except (LookupError, ValueError, PermissionError, SecretError, PublishError, StorageError) as e:
        console.print(f"[red]error:[/] {str(e).strip(chr(39) + chr(34))}")
        raise SystemExit(1) from None
    except typer.Abort:
        console.print("aborted")
        raise SystemExit(1) from None
    except Exception as e:
        if hasattr(e, "show") and hasattr(e, "exit_code"):   # click usage errors (typer vendors click)
            e.show()
            raise SystemExit(e.exit_code) from None
        raise


if __name__ == "__main__":
    main()
