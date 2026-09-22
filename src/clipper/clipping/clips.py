"""Clip lifecycle.

  candidate ─score─▶ scored ─render─▶ rendered ─human─▶ approved ─publish─▶ published
      │                 │                 │                  └────────▶ rejected
      └─ rejected_compliance (deterministic rule broken, or the agent flagged it)
                        render_failed ◀───┘
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import campaigns, db
from ..config import settings
from ..ranking import compliance
from ..ranking.rubric import HOOK_TYPES
from ..ranking.scoring import ClipScore, weighted
from . import render as renderer
from . import sources
from .transcript import Transcript, for_source, snap, window_text

ACTIVE = ("candidate", "scored", "rendering", "rendered", "render_failed", "approved")


class Candidate(BaseModel):
    start: float = Field(ge=0, description="Rough start in seconds; snapped to a word boundary")
    end: float = Field(gt=0, description="Rough end in seconds; snapped to a word boundary")
    title: str = Field(description="Working title, e.g. 'He lost $400k overnight'")
    hook_text: str | None = Field(None, description="On-screen hook card for the first seconds (max ~10 words)")
    hook_type: str = Field("other", description=f"One of: {', '.join(HOOK_TYPES)}")
    topic: str | None = None
    rationale: str | None = Field(None, description="Why this moment works")
    framing: str = Field("crop", description="crop (single speaker) or blur (wide / two-person shot)")


def _get(conn, clip_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    if row is None:
        raise LookupError(f"no clip {clip_id}")
    return dict(row)


def get(clip_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        return _get(conn, clip_id)


def add_candidates(source_id: int, candidates: list[Candidate], origin: str = "agent",
                   transcript: Transcript | None = None, snap_bounds: bool = True,
                   origin_refs: list[str | None] | None = None) -> list[dict[str, Any]]:
    """Snap, check and store candidates. Returns what was stored, including the
    exact words each cut now starts with, so the agent can verify its hook."""
    src = sources.get(source_id)
    spec = campaigns.spec(src["campaign_id"])
    t = transcript or for_source(source_id)
    out: list[dict[str, Any]] = []
    with db.connect() as conn:
        for i, c in enumerate(candidates):
            try:
                start, end = snap(t, c.start, c.end) if snap_bounds else (c.start, c.end)
            except ValueError as e:
                out.append({"title": c.title, "error": str(e)})
                continue
            text = window_text(t, start, end)
            issues = compliance.check_clip(spec, text, end - start)
            hook_type = c.hook_type if c.hook_type in HOOK_TYPES else "other"
            status = "rejected_compliance" if issues else "candidate"
            cur = conn.execute(
                "INSERT INTO clips (campaign_id, source_id, start_time, end_time, title, hook_text, "
                "hook_type, topic, opening_words, transcript, rationale, origin, origin_ref, "
                "compliance_issues, framing, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (src["campaign_id"], source_id, start, end, c.title, c.hook_text, hook_type, c.topic,
                 " ".join(text.split()[:8]), text, c.rationale, origin,
                 (origin_refs or [None] * len(candidates))[i], db.dumps(issues),
                 c.framing if c.framing in ("crop", "blur", "openshorts") else "crop",
                 status, db.now(), db.now()))
            out.append({"clip_id": cur.lastrowid, "title": c.title, "start": start, "end": end,
                        "duration": round(end - start, 1), "opens_with": " ".join(text.split()[:12]),
                        "compliance_issues": issues, "status": status})
    return out


def score(scores: list[ClipScore]) -> list[dict[str, Any]]:
    results = []
    with db.connect() as conn:
        for s in scores:
            clip = _get(conn, s.clip_id)
            spec = campaigns.spec(clip["campaign_id"])
            total = weighted(s, spec.weights)
            issues = db.loads(clip["compliance_issues"], [])
            if not s.compliant:
                issues.append(f"judge: {s.compliance_notes or 'flagged non-compliant'}")
            status = "rejected_compliance" if issues else "scored"
            if clip["status"] in ("rendered", "approved", "published"):
                status = clip["status"]  # re-scoring must not undo later stages
            conn.execute(
                "UPDATE clips SET ai_score=?, hook_score=?, retention_score=?, context_score=?, "
                "emotion_score=?, novelty_score=?, comment_score=?, campaign_fit_score=?, "
                "compliance_score=?, compliance_issues=?, judge_notes=?, status=?, updated_at=? WHERE id=?",
                (total, s.hook, s.retention, s.context, s.emotion, s.novelty, s.comment, s.campaign_fit,
                 0.0 if issues else 100.0, db.dumps(issues), s.notes, status, db.now(), s.clip_id))
            results.append({"clip_id": s.clip_id, "ai_score": total, "status": status})
    return sorted(results, key=lambda r: -r["ai_score"])


def list_clips(campaign_ref: str | int | None = None, status: str | list[str] | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
    where, args = [], []
    if campaign_ref is not None:
        where.append("campaign_id = ?")
        args.append(campaigns.get(campaign_ref)["id"])
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        args += statuses
    sql = "SELECT * FROM clips" + (f" WHERE {' AND '.join(where)}" if where else "")
    sql += " ORDER BY ai_score IS NULL, ai_score DESC, id LIMIT ?"
    with db.connect() as conn:
        return db.rows(conn.execute(sql, (*args, limit)))


def top_unrendered(campaign_ref: str | int, n: int) -> list[dict[str, Any]]:
    return list_clips(campaign_ref, "scored", limit=n)


def update(clip_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE clips SET {cols}, updated_at = ? WHERE id = ?",
                     (*fields.values(), db.now(), clip_id))


def render_clip(clip_id: int, framing: str | None = None, crop_x: float = 0.5) -> dict[str, Any]:
    clip = get(clip_id)
    if clip["status"] == "rejected_compliance":
        raise ValueError(f"clip {clip_id} failed compliance: {clip['compliance_issues']}")
    src = sources.get(clip["source_id"])
    framing = framing or clip["framing"] or "crop"
    out = settings().renders_dir / f"campaign-{clip['campaign_id']}" / f"clip-{clip_id}.mp4"
    update(clip_id, status="rendering", render_error=None, framing=framing)
    try:
        if clip["origin"] == "openshorts" and clip["origin_ref"] and framing == "openshorts":
            from ..integrations import openshorts  # its render: face-tracked, captioned

            openshorts.download_clip(clip["origin_ref"], out)
        else:
            if framing == "openshorts":
                framing = "crop"
            t = for_source(clip["source_id"])
            renderer.render(src["path"], out, clip["start_time"], clip["end_time"], t,
                            framing=framing, crop_x=crop_x, captions=True,
                            hook_text=clip["hook_text"], font_path=settings().font_path)
    except Exception as e:
        update(clip_id, status="render_failed", render_error=str(e)[:1000])
        raise
    # Captions are always burned in; `required: subtitles` is satisfied by construction.
    status = "approved" if clip["status"] == "approved" else "rendered"
    update(clip_id, status=status, video_path=str(out))
    return get(clip_id)


def set_review(clip_ids: list[int], decision: str) -> list[dict[str, Any]]:
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    out = []
    for cid in clip_ids:
        clip = get(cid)
        if decision == "approved" and not (clip["video_path"] and Path(clip["video_path"]).exists()):
            raise ValueError(f"clip {cid} has no rendered video; render it before approving")
        update(cid, status=decision)
        out.append({"clip_id": cid, "status": decision})
    return out


class PlatformCopy(BaseModel):
    title: str | None = Field(None, description="YouTube title / headline")
    caption: str = Field(description="Post caption including required hashtags")
    description: str | None = None


def set_copy(clip_id: int, copy: dict[str, PlatformCopy]) -> dict[str, Any]:
    clip = get(clip_id)
    spec = campaigns.spec(clip["campaign_id"])
    payload = {p: c.model_dump() for p, c in copy.items()}
    issues = compliance.check_copy(spec, payload, list(copy))
    update(clip_id, copy_json=db.dumps(payload))
    return {"clip_id": clip_id, "copy": payload, "issues": issues}
