"""Performance feedback: what the published record says about which clip
characteristics earn qualified views, and how that feeds back into ranking.

Methods are deliberately simple and honest about uncertainty:
  cohorts      mean log-views per trait value, shrunk toward the overall mean
               (normal-normal, pseudo-count K) with an 80% interval
  regression   ridge regression on log-views with one-hot traits + numeric
               factors, once n >= 15 (standardized coefficients)
  calibration  Spearman correlation between Ezra's rank score and views
  prior        a candidate's expected log-views from its traits' shrunken
               cohort effects, as a smooth 0-100 score (50 = typical) with a confidence that
               grows with data. Used by the PerformanceCritic.
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean
from typing import Any

import numpy as np
from sqlalchemy import select

from . import db
from .db.models import Learning, MetricSnapshot, Post

K = 5.0            # shrinkage pseudo-count
TRAITS = ("hook_type", "duration_bucket", "layout", "caption_theme", "platform", "opening", "topic",
          "posting_hour_bucket", "weekday", "speaker", "has_cta", "silence_removed")
NUMERIC = ("rank_score", "hook", "retention", "context", "emotion", "novelty", "discussion", "payoff",
           "visual", "duration")


def duration_bucket(d: float) -> str:
    return "<20s" if d < 20 else "20-30s" if d < 30 else "30-45s" if d < 45 else "45s+"


def hour_bucket(h: int) -> str:
    return "night" if h < 6 else "morning" if h < 12 else "afternoon" if h < 18 else "evening"


def observations(campaign_id: int | None = None, include_private: bool = False) -> list[dict[str, Any]]:
    """One row per published post with its latest views and the features that
    produced it (snapshotted on the post at publish time)."""
    with db.session() as s:
        q = select(Post).where(Post.status == "published")
        if campaign_id is not None:
            q = q.where(Post.campaign_id == campaign_id)
        if not include_private:
            q = q.where(Post.visibility == "public")
        posts = list(s.scalars(q.order_by(Post.id)))
        out = []
        for p in posts:
            snap = s.scalar(select(MetricSnapshot).where(MetricSnapshot.post_id == p.id,
                                                         MetricSnapshot.views.is_not(None))
                            .order_by(MetricSnapshot.captured_at.desc(), MetricSnapshot.id.desc()).limit(1))
            if snap is None:
                continue
            f = dict(p.features or {})
            f.update(post_id=p.id, clip_id=p.clip_id, platform=p.platform, views=snap.views,
                     likes=snap.likes, comments=snap.comments, shares=snap.shares, saves=snap.saves,
                     metrics_provider=snap.provider, experiment_variant=p.experiment_variant)
            if "duration" in f:
                f["duration_bucket"] = duration_bucket(float(f["duration"]))
            if p.published_at:
                pub = db.aware(p.published_at)
                f.setdefault("posting_hour_bucket", hour_bucket(pub.hour))  # type: ignore[union-attr]
                f.setdefault("weekday", pub.strftime("%a"))  # type: ignore[union-attr]
            out.append(f)
        return out


def _shrunk(values: list[float], prior_mean: float, prior_var: float) -> tuple[float, float]:
    n = len(values)
    m = mean(values)
    post_mean = (n * m + K * prior_mean) / (n + K)
    post_sd = math.sqrt(prior_var / (n + K))
    return post_mean, post_sd


def cohorts(obs: list[dict[str, Any]], min_n: int = 2) -> list[dict[str, Any]]:
    if not obs:
        return []
    logs = [math.log(o["views"] + 1) for o in obs]
    overall, var = mean(logs), (np.var(logs) if len(logs) > 1 else 1.0) or 1.0
    rows = []
    for trait in TRAITS:
        groups: dict[str, list[float]] = defaultdict(list)
        for o, lv in zip(obs, logs):
            v = o.get(trait)
            if v is not None and v != "":
                groups[str(v)].append(lv)
        if len(groups) < 2:
            continue
        for value, vals in groups.items():
            if len(vals) < min_n:
                continue
            m, sd = _shrunk(vals, overall, float(var))
            rows.append({"trait": trait, "value": value, "n": len(vals),
                         "median_views": int(math.exp(float(np.median(vals))) - 1),
                         "shrunk_multiplier": round(math.exp(m - overall), 2),
                         "interval80": [round(math.exp(m - 1.28 * sd - overall), 2),
                                        round(math.exp(m + 1.28 * sd - overall), 2)]})
    return sorted(rows, key=lambda r: -abs(math.log(r["shrunk_multiplier"])))


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None

    def ranks(v: list[float]) -> np.ndarray:
        order = np.argsort(v, kind="mergesort")
        r = np.empty(len(v))
        r[order] = np.arange(len(v))
        # average ties
        vals = np.array(v)
        for u in np.unique(vals):
            idx = vals == u
            r[idx] = r[idx].mean()
        return r

    a, b = ranks(xs), ranks(ys)
    if a.std() == 0 or b.std() == 0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 3)


def regression(obs: list[dict[str, Any]], alpha: float = 1.0) -> dict[str, Any] | None:
    if len(obs) < 15:
        return None
    cats = [t for t in ("hook_type", "duration_bucket", "layout", "caption_theme", "platform") if
            len({o.get(t) for o in obs}) > 1]
    columns: list[str] = []
    rows: list[list[float]] = []
    levels = {t: sorted({str(o.get(t)) for o in obs})[1:] for t in cats}
    for t in cats:
        columns += [f"{t}={lv}" for lv in levels[t]]
    nums = [n for n in NUMERIC if all(o.get(n) is not None for o in obs)]
    columns += nums
    for o in obs:
        row = [1.0 if str(o.get(t)) == lv else 0.0 for t in cats for lv in levels[t]]
        row += [float(o[n]) for n in nums]
        rows.append(row)
    X = np.array(rows)
    y = np.array([math.log(o["views"] + 1) for o in obs])
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1
    Z = (X - mu) / sd
    coef = np.linalg.solve(Z.T @ Z + alpha * np.eye(Z.shape[1]), Z.T @ (y - y.mean()))
    pred = Z @ coef + y.mean()
    r2 = 1 - float(((y - pred) ** 2).sum() / max(1e-9, ((y - y.mean()) ** 2).sum()))
    return {"n": len(obs), "r2_in_sample": round(r2, 3),
            "coefficients": sorted(({"feature": c, "std_effect": round(float(b), 3)} for c, b in zip(columns, coef)),
                                   key=lambda r: -abs(r["std_effect"]))}


def insights(campaign_id: int | None = None, min_n: int = 2) -> dict[str, Any]:
    obs = observations(campaign_id)
    if not obs:
        return {"n_posts": 0, "cohorts": [], "calibration": None, "regression": None,
                "observations": ["No published posts with metrics yet. Publish, then `ezra metrics sync`."],
                "learnings": [learning_dict(x) for x in learnings()]}
    coh = cohorts(obs, min_n)
    scored = [(o["rank_score"], o["views"]) for o in obs if o.get("rank_score") is not None]
    rho = spearman([a for a, _ in scored], [float(b) for _, b in scored])
    return {"n_posts": len(obs), "median_views": int(np.median([o["views"] for o in obs])),
            "cohorts": coh,
            "calibration": {"spearman_rank_vs_views": rho, "n": len(scored),
                            "reading": None if rho is None else ("ranking tracks views" if rho >= 0.4 else
                                                                 "weak link between rank and views" if rho >= 0.1
                                                                 else "rank does not predict views yet")},
            "regression": regression(obs),
            "observations": actionable(coh, rho, len(obs)),
            "learnings": [learning_dict(x) for x in learnings()]}


def actionable(coh: list[dict[str, Any]], rho: float | None, n: int) -> list[str]:
    """Plain statements, only where the 80% interval excludes 'no effect'."""
    out = []
    for r in coh:
        lo, hi = r["interval80"]
        if lo > 1.0:
            out.append(f"{r['trait'].replace('_', ' ')} = {r['value']}: about {r['shrunk_multiplier']}x typical "
                       f"views (n={r['n']}, 80% interval {lo}-{hi}x). Favour it.")
        elif hi < 1.0:
            out.append(f"{r['trait'].replace('_', ' ')} = {r['value']}: about {r['shrunk_multiplier']}x typical "
                       f"views (n={r['n']}, 80% interval {lo}-{hi}x). Use less.")
    if rho is not None and rho < 0.1 and n >= 10:
        out.append("Ezra's rank score is not predicting views for these posts; review the scoring weights.")
    if not out:
        out.append(f"No trait separates from the average yet with {n} posts; keep posting variety "
                   f"(or run an experiment) before drawing conclusions.")
    return out


def prior(features: dict[str, Any], campaign_id: int | None = None) -> tuple[float, float, str]:
    """Performance prior for a candidate: (0-100 percentile, confidence, basis)."""
    obs = observations(campaign_id) or observations(None)
    if len(obs) < 3:
        return 50.0, 0.0, "no history"
    logs = [math.log(o["views"] + 1) for o in obs]
    overall = mean(logs)
    var = float(np.var(logs)) or 1.0
    f = dict(features)
    if "duration" in f and "duration_bucket" not in f:
        f["duration_bucket"] = duration_bucket(float(f["duration"]))
    effects, used = [], []
    for trait in ("hook_type", "duration_bucket", "layout", "caption_theme", "opening", "topic"):
        v = f.get(trait)
        if v is None:
            continue
        vals = [lv for o, lv in zip(obs, logs) if str(o.get(trait)) == str(v)]
        if vals:
            m, _ = _shrunk(vals, overall, var)
            effects.append(m - overall)
            used.append(f"{trait}={v} (n={len(vals)})")
    shift = sum(effects) / len(effects) if effects else 0.0
    # Smooth 0-100 score: 50 = typical, +/-1 sd of log-views -> ~88/12. (A rank
    # percentile collapses on clustered results: every value between two clusters
    # maps to the same number.)
    sd = math.sqrt(var)
    score = 50 + 50 * math.tanh(shift / sd) if sd > 0 else 50.0
    conf = round(min(0.8, len(obs) / 60), 2)
    return round(score, 1), conf, ", ".join(used) or "overall average"


def learnings(active_only: bool = True) -> list[Learning]:
    with db.session() as s:
        q = select(Learning).order_by(Learning.id)
        if active_only:
            q = q.where(Learning.active.is_(True))
        return list(s.scalars(q))


def save_learning(text: str, evidence: dict[str, Any] | None = None, origin: str = "analyst") -> Learning:
    with db.session() as s:
        row = Learning(text=text, evidence=evidence or {}, origin=origin)
        s.add(row)
        s.flush()
        return row


def retire_learning(learning_id: int) -> None:
    with db.session() as s:
        row = s.get(Learning, learning_id)
        if row is None:
            raise LookupError(f"no learning {learning_id}")
        row.active = False


def learning_dict(x: Learning) -> dict[str, Any]:
    return {"id": x.id, "text": x.text, "evidence": x.evidence, "origin": x.origin, "active": x.active}
