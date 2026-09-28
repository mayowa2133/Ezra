"""Moment-selection benchmark on real footage: how well Ezra's own (heuristic, no model) ranking
finds moments a person judged strong, and how often its top picks repeat a story.

Reference set: candidates with origin 'agent' that became clips on the given sources (moments chosen
by reading the full transcript). A scout candidate "finds" a reference moment when it covers at least half of it;
it "opens on" it when it starts within 2 s of the reference start.

    uv run python scripts/selection_bench.py --sources 3 4 [--top 10 20] [--rescout]
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import select

from ezra import candidates, db
from ezra.db.models import Candidate, Clip


def cover(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Share of b covered by a."""
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0])) / max(1e-6, b[1] - b[0])


def measure(source_ids: list[int], tops: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with db.session() as s:
        for sid in source_ids:
            # the moments a person picked and rendered (re-cuts that replaced a pick don't count twice)
            clipped = select(Clip.candidate_id)
            refs = list(s.scalars(select(Candidate).where(Candidate.source_id == sid, Candidate.origin == "agent",
                                                          Candidate.id.in_(clipped))))
            scouts = sorted(s.scalars(select(Candidate).where(Candidate.source_id == sid, Candidate.origin == "scout",
                                                              Candidate.compliance_status != "FAIL")),
                            key=lambda c: -(c.rank_score or 0))
            row: dict[str, Any] = {"reference_moments": len(refs), "scout_candidates": len(scouts)}
            for k in tops:
                top = scouts[:k]
                found = [r for r in refs if any(cover((c.start, c.end), (r.start, r.end)) >= 0.5 for c in top)]
                opens = [r for r in refs if any(abs(c.start - r.start) <= 2.0 for c in top)]
                dups = sum(1 for i, a in enumerate(top) for b in top[i + 1:]
                           if cover((a.start, a.end), (b.start, b.end)) > 0.5
                           or cover((b.start, b.end), (a.start, a.end)) > 0.5
                           or candidates.story_similarity(a.transcript or "", b.transcript or "") >= 0.35)
                row[f"top{k}"] = {"found": len(found), "opens_on": len(opens), "duplicate_pairs": dups,
                                  "recall": round(len(found) / max(1, len(refs)), 3),
                                  "opening_recall": round(len(opens) / max(1, len(refs)), 3)}
            out[str(sid)] = row
    per_source = {k: v for k, v in out.items()}
    tot = sum(v["reference_moments"] for v in per_source.values())
    for k in tops:
        out[f"all_top{k}"] = {
            "recall": round(sum(v[f"top{k}"]["found"] for v in per_source.values()) / max(1, tot), 3),
            "opening_recall": round(sum(v[f"top{k}"]["opens_on"] for v in per_source.values()) / max(1, tot), 3),
            "duplicate_pairs": sum(v[f"top{k}"]["duplicate_pairs"] for v in per_source.values())}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", type=int, nargs="+", required=True)
    ap.add_argument("--top", type=int, nargs="+", default=[10, 20])
    ap.add_argument("--rescout", action="store_true", help="re-run candidate generation (heuristic, no model)")
    a = ap.parse_args()
    if a.rescout:
        for sid in a.sources:
            candidates.find_candidates(sid, max_candidates=40)
    print(json.dumps(measure(a.sources, a.top), indent=2))
