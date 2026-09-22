"""What the numbers say about past clips. clipper computes the statistics;
the agent (performance-analyst) interprets them and saves learnings, which
flow back into every future judging brief."""

from __future__ import annotations

import re
from collections import defaultdict
from statistics import mean
from typing import Any

from .. import db, revenue

DURATION_BUCKETS = [(0, 22, "<22s"), (22, 34, "22-34s"), (34, 45, "34-45s"), (45, 10_000, "45s+")]


def _duration_bucket(seconds: float) -> str:
    for lo, hi, label in DURATION_BUCKETS:
        if lo <= seconds < hi:
            return label
    return "?"


def _opening(words: str | None) -> str:
    toks = re.findall(r"[a-z0-9$']+", (words or "").lower())
    return " ".join(toks[:2]) or "?"


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(num / den, 3) if den else None


def analyze(campaign_ref: str | int | None = None, min_n: int = 3) -> dict[str, Any]:
    posts = [p for p in revenue.posts_with_latest(campaign_ref) if p["metrics_at"]]
    if not posts:
        return {"n_posts": 0, "patterns": [], "calibration": None, "learnings": learnings(),
                "note": "No metrics yet. Record some with `clipper metrics add` or `clipper metrics sync`."}
    overall = mean(p["views"] for p in posts)
    features = {
        "hook_type": lambda p: p["hook_type"] or "other",
        "opening_words": lambda p: _opening(p["opening_words"]),
        "duration": lambda p: _duration_bucket(p["end_time"] - p["start_time"]),
        "platform": lambda p: p["platform"],
        "topic": lambda p: (p["topic"] or "?").lower(),
    }
    patterns = []
    for feature, key in features.items():
        groups: dict[str, list[int]] = defaultdict(list)
        for p in posts:
            groups[key(p)].append(p["views"])
        for value, views in groups.items():
            if len(views) < min_n or value == "?":
                continue
            avg = mean(views)
            patterns.append({
                "feature": feature, "value": value, "n": len(views), "avg_views": round(avg),
                "lift_pct": round((avg / overall - 1) * 100, 1) if overall else 0.0,
            })
    patterns.sort(key=lambda r: -abs(r["lift_pct"]))
    scored = [p for p in posts if p["ai_score"] is not None]
    rho = spearman([p["ai_score"] for p in scored], [float(p["views"]) for p in scored])
    return {
        "n_posts": len(posts), "overall_avg_views": round(overall), "patterns": patterns,
        "calibration": {"spearman_ai_score_vs_views": rho, "n": len(scored),
                        "reading": None if rho is None else (
                            "judge scores track views" if rho >= 0.4 else
                            "weak link between judge scores and views" if rho >= 0.1 else
                            "judge scores do not predict views; revisit the rubric")},
        "learnings": learnings(),
    }


def for_brief(limit: int = 8) -> dict[str, Any]:
    a = analyze()
    return {
        "n_posts_analyzed": a["n_posts"],
        "patterns": [
            f"{r['feature']} = '{r['value']}': avg {r['avg_views']:,} views ({r['lift_pct']:+.0f}% vs average, n={r['n']})"
            for r in a["patterns"][:limit]
        ],
        "judge_calibration": a["calibration"],
        "learnings": [l["text"] for l in a["learnings"]],
        "how_to_use": "Favour moments that share traits with past winners; be sceptical of traits that "
                      "keep underperforming. Small n means weak evidence.",
    }


def learnings(active_only: bool = True) -> list[dict[str, Any]]:
    with db.connect() as conn:
        sql = "SELECT * FROM learnings" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        return db.rows(conn.execute(sql))


def save_learning(text: str, evidence: str | None = None) -> dict[str, Any]:
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO learnings (text, evidence, created_at) VALUES (?,?,?)",
                           (text, evidence, db.now()))
        return dict(conn.execute("SELECT * FROM learnings WHERE id = ?", (cur.lastrowid,)).fetchone())


def retire_learning(learning_id: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE learnings SET active = 0 WHERE id = ?", (learning_id,))
