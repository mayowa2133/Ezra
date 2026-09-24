"""Experiments: controlled comparisons of one editing/posting factor.

Design is between-clip: each clip is assigned exactly one variant (round-robin),
so no piece of content is posted twice to the same account (no duplicate spam),
and campaign/platform rules still apply to every post.

Factors: caption_theme, layout, cta (on/off or text), punch_in, hook_overlay,
posting_time (hour of day). Analysis: per-variant mean log-views with a normal
posterior; P(best) by sampling; a winner is declared only when every variant
has min_samples_per_variant posts and P(best) >= 0.9.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import select

from . import analytics, campaigns, db, render
from .db.models import Experiment, Post, utcnow
from .render.captions import THEMES
from .render.spec import LAYOUTS, PUNCH_INS

FACTORS = {
    "caption_theme": lambda v: v in THEMES,
    "layout": lambda v: v in LAYOUTS,
    "punch_in": lambda v: v in PUNCH_INS,
    "hook_overlay": lambda v: isinstance(v, bool),
    "caption_emoji": lambda v: isinstance(v, bool),
    "cta": lambda v: v is None or isinstance(v, str),
    "posting_time": lambda v: isinstance(v, int) and 0 <= v <= 23,
}


def create(campaign: str | int | None, name: str, factor: str, values: list[Any], hypothesis: str | None = None,
           min_samples: int = 5) -> Experiment:
    if factor not in FACTORS:
        raise ValueError(f"factor must be one of {sorted(FACTORS)}")
    if len(values) < 2:
        raise ValueError("an experiment needs at least two variants")
    bad = [v for v in values if not FACTORS[factor](v)]
    if bad:
        raise ValueError(f"invalid values for {factor}: {bad}")
    camp = campaigns.get(campaign) if campaign is not None else None
    with db.session() as s:
        e = Experiment(campaign_id=camp.id if camp else None, name=name, factor=factor, hypothesis=hypothesis,
                       variants=[{"key": chr(65 + i), "value": v} for i, v in enumerate(values)],
                       min_samples_per_variant=min_samples)
        s.add(e)
        s.flush()
        return e


def get(experiment_id: int) -> Experiment:
    with db.session() as s:
        e = s.get(Experiment, experiment_id)
        if e is None:
            raise LookupError(f"no experiment {experiment_id}")
        return e


def list_experiments(campaign: str | int | None = None) -> list[Experiment]:
    with db.session() as s:
        q = select(Experiment).order_by(Experiment.id)
        if campaign is not None:
            q = q.where(Experiment.campaign_id == campaigns.get(campaign).id)
        return list(s.scalars(q))


def assignments(e: Experiment) -> dict[str, int]:
    with db.session() as s:
        rows = list(s.scalars(select(Post).where(Post.experiment_id == e.id)))
    counts = {v["key"]: 0 for v in e.variants}
    seen: set[tuple[int, str]] = set()
    for p in rows:
        if p.experiment_variant and (p.clip_id, p.experiment_variant) not in seen:
            counts[p.experiment_variant] = counts.get(p.experiment_variant, 0) + 1
            seen.add((p.clip_id, p.experiment_variant))
    return counts


def assign(experiment_id: int, clip_id: int, tz: str = "UTC") -> dict[str, Any]:
    """Pick the least-used variant for this clip and apply it (re-render for
    editing factors, a schedule time for posting_time)."""
    e = get(experiment_id)
    if e.status != "running":
        raise ValueError(f"experiment {experiment_id} is {e.status}")
    clip = render.get_clip(clip_id)
    if e.campaign_id and clip.campaign_id != e.campaign_id:
        raise ValueError("clip belongs to a different campaign")
    counts = assignments(e)
    variant = min(e.variants, key=lambda v: (counts.get(v["key"], 0), v["key"]))
    out: dict[str, Any] = {"experiment_id": e.id, "clip_id": clip_id, "variant": variant["key"],
                           "factor": e.factor, "value": variant["value"]}
    if e.factor == "posting_time":
        now = datetime.now(ZoneInfo(tz))
        when = now.replace(hour=variant["value"], minute=0, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)
        out["schedule_at"] = when.isoformat()
    else:
        change = {("cta_text" if e.factor == "cta" else e.factor): variant["value"], "variant": variant["key"]}
        clip = render.rerender(clip_id, change)
        out["clip_version"] = render.current_version(clip).version  # type: ignore[union-attr]
    return out


def analyze(experiment_id: int, samples: int = 20000, seed: int = 11) -> dict[str, Any]:
    e = get(experiment_id)
    obs = [o for o in analytics.observations(e.campaign_id) if o.get("experiment_variant")]
    with db.session() as s:
        ids = {p.id for p in s.scalars(select(Post).where(Post.experiment_id == e.id))}
    obs = [o for o in obs if o["post_id"] in ids]
    rng = np.random.default_rng(seed)
    rows, draws = [], {}
    all_logs = [math.log(o["views"] + 1) for o in obs] or [math.log(1000)]
    prior_mu, prior_var = float(np.mean(all_logs)), max(float(np.var(all_logs)), 1.0)
    for v in e.variants:
        logs = [math.log(o["views"] + 1) for o in obs if o["experiment_variant"] == v["key"]]
        n = len(logs)
        var = float(np.var(logs)) if n > 1 else prior_var
        post_var = 1 / (1 / prior_var + n / max(var, 1e-6)) if n else prior_var
        post_mu = post_var * (prior_mu / prior_var + (sum(logs) / max(var, 1e-6) if n else 0))
        draws[v["key"]] = rng.normal(post_mu, math.sqrt(post_var), samples)
        rows.append({"variant": v["key"], "value": v["value"], "n": n,
                     "median_views": int(math.exp(float(np.median(logs))) - 1) if n else None,
                     "posterior_median_views": int(math.exp(post_mu) - 1)})
    stacked = np.vstack([draws[r["variant"]] for r in rows])
    best = np.bincount(stacked.argmax(0), minlength=len(rows)) / samples
    for r, p in zip(rows, best):
        r["p_best"] = round(float(p), 3)
    enough = all(r["n"] >= e.min_samples_per_variant for r in rows)
    leader = max(rows, key=lambda r: r["p_best"])
    verdict = (f"variant {leader['variant']} ({leader['value']}) wins with P(best)={leader['p_best']}"
               if enough and leader["p_best"] >= 0.9 else
               "not enough evidence yet" + ("" if enough else f" (need {e.min_samples_per_variant} posts per variant)"))
    return {"experiment_id": e.id, "name": e.name, "factor": e.factor, "status": e.status, "variants": rows,
            "decided": enough and leader["p_best"] >= 0.9, "verdict": verdict}


def stop(experiment_id: int) -> Experiment:
    with db.session() as s:
        e = s.get(Experiment, experiment_id)
        if e is None:
            raise LookupError(f"no experiment {experiment_id}")
        e.status = "stopped"
        e.updated_at = utcnow()
        return e


def to_dict(e: Experiment) -> dict[str, Any]:
    return {"id": e.id, "campaign_id": e.campaign_id, "name": e.name, "factor": e.factor,
            "hypothesis": e.hypothesis, "variants": e.variants, "status": e.status,
            "min_samples_per_variant": e.min_samples_per_variant, "assignments": assignments(e)}
