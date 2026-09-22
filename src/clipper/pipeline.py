"""`clipper run <campaign>`: the whole loop from footage to posts.

  transcribe (local) → agent finds + judges (MCP) → render top N (local)
  → human approves in the terminal → agent writes copy (MCP)
  → human confirms → publish (Upload-Post) → tracking
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn

from . import campaigns, db
from .agents import prompts
from .agents import runner as agent_runner
from .clipping import clips, sources
from .clipping.transcript import fmt_ts

AgentFn = Callable[[str], str]
AskFn = Callable[[str], str]


@dataclass
class RunOptions:
    candidates: int = 20
    render: int = 5
    platforms: list[str] | None = None
    agent: str | None = None
    model: str | None = None
    openshorts: bool = False
    review: bool = True
    publish: bool = True
    framing: str | None = None
    extra: dict = field(default_factory=dict)


def parse_selection(answer: str, n: int) -> list[int]:
    """'1,2' / '1 3' / '1-3' / 'all' / '' → zero-based indexes."""
    answer = answer.strip().lower()
    if answer in ("all", "*"):
        return list(range(n))
    picked: set[int] = set()
    for part in answer.replace(",", " ").split():
        if "-" in part:
            a, b = part.split("-", 1)
            picked.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            picked.add(int(part))
    bad = [p for p in picked if not 1 <= p <= n]
    if bad:
        raise ValueError(f"no clip number {bad[0]} (choose 1-{n})")
    return sorted(p - 1 for p in picked)


def _transcribe_pending(campaign: str, console: Console) -> None:
    from .clipping.transcribe import transcribe

    for src in sources.list_for(campaign):
        if src["status"] == "transcribed":
            continue
        with Progress(TextColumn("  transcribing {task.description}"), BarColumn(),
                      TextColumn("{task.percentage:>3.0f}%"), console=console) as bar:
            task = bar.add_task(src["title"] or f"source {src['id']}", total=1.0)
            transcribe(src["id"], progress=lambda f: bar.update(task, completed=f))


def run(campaign_ref: str, opts: RunOptions, console: Console | None = None,
        agent: AgentFn | None = None, ask: AskFn | None = None, uploader=None) -> dict:
    console = console or Console()
    ask = ask or (lambda q: console.input(q))

    def default_agent(prompt: str) -> str:
        with console.status("[dim]agent working…[/]") as status:
            return agent_runner.run(prompt, opts.agent, opts.model,
                                    on_event=lambda m: status.update(f"[dim]agent: {m}[/]"))

    agent = agent or default_agent
    camp = campaigns.get(campaign_ref)
    spec = campaigns.spec(campaign_ref)
    slug = camp["slug"]
    platforms = opts.platforms or spec.platforms
    srcs = sources.list_for(slug)
    if not srcs:
        raise RuntimeError(f"campaign {slug} has no sources: `clipper source add {slug} <video>`")

    console.print(f"\n[bold]Campaign:[/] {spec.name}")
    console.print(f"[bold]CPM:[/] ${spec.rate.cpm:.2f}")
    console.print(f"[bold]Source:[/] {', '.join(s['title'] or s['path'] for s in srcs)}\n")

    _transcribe_pending(slug, console)
    if opts.openshorts:
        from .integrations import openshorts

        for src in sources.list_for(slug):
            if not any(c["origin"] == "openshorts" for c in clips.list_clips(slug) if c["source_id"] == src["id"]):
                with console.status("[dim]OpenShorts processing…[/]") as st:
                    openshorts.run(src["id"], on_log=lambda l: st.update(f"[dim]OpenShorts: {l[:80]}[/]"))

    needs_agent = any(s["n_clips"] == 0 for s in sources.list_for(slug)) or clips.list_clips(slug, "candidate")
    if needs_agent:
        summary = agent(prompts.FIND_AND_JUDGE.format(campaign=slug, n=opts.candidates))
        if summary:
            console.print(f"[dim]{summary.strip()}[/]\n")

    all_clips = clips.list_clips(slug, limit=10_000)
    analyzed = sum(s["duration"] or 0 for s in sources.list_for(slug))
    to_render = clips.top_unrendered(slug, opts.render)
    for c in to_render:
        with console.status(f"[dim]rendering clip {c['id']}…[/]"):
            try:
                clips.render_clip(c["id"], opts.framing)
            except Exception as e:  # keep going; the failure is recorded on the clip
                console.print(f"[red]render failed for clip {c['id']}: {e}[/]")

    console.print(f"[bold]Analyzed:[/] {fmt_ts(analyzed)}")
    console.print(f"[bold]Candidate moments:[/] {len(all_clips)}")
    console.print(f"[bold]Rendered:[/] {len(clips.list_clips(slug, ['rendered', 'approved', 'published'], 10_000))}\n")

    pending = clips.list_clips(slug, "rendered")
    if not pending:
        console.print("No rendered clips waiting for review.")
        return {"campaign": slug, "approved": [], "published": []}
    console.print("[bold]Top clips:[/]\n")
    for i, c in enumerate(pending, 1):
        console.print(f"{i}. \"{c['title']}\"")
        console.print(f"   AI score: {c['ai_score']:.0f}/100")
        console.print(f"   Duration: {c['end_time'] - c['start_time']:.0f}s")
        if c["judge_notes"]:
            console.print(f"   [dim]{c['judge_notes']}[/]")
        console.print(f"   [dim]{c['video_path']}[/]\n")
    if not opts.review:
        return {"campaign": slug, "approved": [], "published": [], "awaiting_review": [c["id"] for c in pending]}

    while True:
        try:
            idx = parse_selection(ask(f"Approve clips? [{','.join(str(i) for i in range(1, len(pending) + 1))}] "), len(pending))
            break
        except ValueError as e:
            console.print(f"[red]{e}[/]")
    approved = [pending[i]["id"] for i in idx]
    rejected = [c["id"] for i, c in enumerate(pending) if i not in idx]
    if approved:
        clips.set_review(approved, "approved")
    if rejected:
        clips.set_review(rejected, "rejected")
    if not approved:
        console.print("Nothing approved.")
        return {"campaign": slug, "approved": [], "published": []}

    console.print("\nGenerating platform copy...\n")
    agent(prompts.WRITE_COPY.format(campaign=slug, clip_ids=approved, platforms=platforms))
    for cid in approved:
        copy = db.loads(clips.get(cid)["copy_json"], {})
        console.print(f"[bold]Clip {cid}[/]")
        for p in platforms:
            entry = copy.get(p) or {}
            console.print(f"  {p.capitalize()}: {entry.get('title') or entry.get('caption') or '[red](missing)[/]'}")
        console.print()

    if not opts.publish:
        return {"campaign": slug, "approved": approved, "published": []}
    from .config import settings
    from .publishing import publisher

    if uploader is None and not (settings().upload_post_api_key and settings().upload_post_user):
        console.print("Upload-Post isn't configured (UPLOAD_POST_API_KEY / UPLOAD_POST_USER), so the "
                      "approved clips stay ready to post: `clipper publish " + slug + "`.")
        return {"campaign": slug, "approved": approved, "published": []}
    if ask("Publish now? [y/N] ").strip().lower() not in ("y", "yes"):
        console.print(f"Not published. Later: `clipper publish {slug}`")
        return {"campaign": slug, "approved": approved, "published": []}

    published = []
    for cid in approved:
        res = publisher.publish(cid, platforms, confirm=True, client=uploader)
        if not res["published"]:
            console.print(f"[red]clip {cid} blocked:[/] {'; '.join(res['problems'])}")
            continue
        for p in res["posts"]:
            mark = "[red]✗[/]" if p["status"] == "failed" else "[green]✓[/]"
            console.print(f"{mark} {p['platform'].capitalize()} {p['status']}"
                          + (f" — {p['error']}" if p.get("error") else ""))
        published.append(cid)
    if published:
        console.print("\nTracking enabled. `clipper metrics sync` pulls views; "
                      "`clipper report " + slug + "` shows revenue.")
    return {"campaign": slug, "approved": approved, "published": published}
