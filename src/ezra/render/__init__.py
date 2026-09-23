"""Render service: candidate → Clip + ClipVersion (video, thumbnail, SRT, ASS)."""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .. import analysis, audit, campaigns, candidates, compliance, costs, db, sources
from ..config import get_settings
from ..db.models import BrandKit, Candidate, Clip, ClipVersion
from ..storage import get_storage
from ..transcription import load_words
from .compose import RenderError, compose
from .spec import PLATFORM_PRESETS, RenderSpec, spec_from_brand

Progress = Callable[[float, str], None]
__all__ = ["RenderSpec", "RenderError", "render_candidate", "render_top", "rerender", "create_variant",
           "export_clip", "get_clip", "list_clips", "clip_dict", "version_dict"]


def _default_spec(cand: Candidate, overrides: dict[str, Any] | None) -> RenderSpec:
    camp = campaigns.get(cand.campaign_id) if cand.campaign_id else None
    base = RenderSpec.model_validate(overrides or {})
    data = base.model_dump()
    if "hook_text" not in base.model_fields_set:
        data["hook_text"] = cand.hook
    if not data.get("highlight_keywords"):
        hits = (cand.features or {}).get("lexicon_hits") or {}
        data["highlight_keywords"] = sorted({w for v in hits.values() for w in v})[:12]
    if camp and camp.subtitles_required:
        data["captions"] = True
    spec = RenderSpec.model_validate(data)
    brand = None
    if spec.brand_kit_id:
        with db.session() as s:
            brand = s.get(BrandKit, spec.brand_kit_id)
    elif camp and camp.brand_kit_id:
        with db.session() as s:
            brand = s.get(BrandKit, camp.brand_kit_id)
    return spec_from_brand(spec, brand, explicit=base.model_fields_set)


def render_candidate(candidate_id: int, spec: dict[str, Any] | RenderSpec | None = None,
                     progress: Progress | None = None) -> Clip:
    say = progress or (lambda f, m: None)
    cand = candidates.get(candidate_id)
    if cand.compliance_status == "FAIL":
        raise PermissionError(f"candidate {candidate_id} fails campaign compliance: "
                              f"{'; '.join(r['message'] for r in cand.compliance_reasons)}")
    rs = spec if isinstance(spec, RenderSpec) else _default_spec(cand, spec)
    camp = campaigns.get(cand.campaign_id) if cand.campaign_id else None
    render_check = compliance.evaluate(camp, "render", render_spec=rs.model_dump())
    if render_check["status"] == "FAIL":
        raise PermissionError("render spec breaks campaign rules: " +
                              "; ".join(r["message"] for r in render_check["reasons"]))
    src = sources.get(cand.source_id)
    words = load_words(cand.source_id)
    scenes = analysis.get(cand.source_id, "scenes")
    cuts = [sc["start"] for sc in (scenes.data.get("scenes", []) if scenes else [])][1:]
    with db.session() as s:
        clip = s.scalar(select(Clip).where(Clip.candidate_id == candidate_id))
        if clip is None:
            clip = Clip(candidate_id=candidate_id, campaign_id=cand.campaign_id, source_id=cand.source_id,
                        title=cand.title, status="rendering")
            s.add(clip)
            s.flush()
        n = (s.scalar(select(func.max(ClipVersion.version)).where(ClipVersion.clip_id == clip.id)) or 0) + 1
        ver = ClipVersion(clip_id=clip.id, version=n, spec=rs.model_dump(), status="pending")
        s.add(ver)
        s.flush()
        clip_id, ver_id, vnum = clip.id, ver.id, n
        if clip.status not in ("approved", "published"):
            clip.status = "rendering"
    work = get_settings().work_dir / f"clip-{clip_id}-v{vnum}"
    work.mkdir(parents=True, exist_ok=True)
    storage = get_storage()
    t0 = time.time()
    try:
        result = compose(sources.local_path(src), cand.start, cand.end, words, cuts, rs, work / "clip.mp4",
                         storage.local_path, progress=lambda f, m: say(f, m))
    except Exception as e:
        with db.session() as s:
            v = s.get(ClipVersion, ver_id)
            v.status, v.error = "failed", f"{type(e).__name__}: {e}"[:4000]  # type: ignore[union-attr]
            c = s.get(Clip, clip_id)
            if c and c.status == "rendering":
                c.status = "failed"
        raise
    prefix = f"clips/{clip_id}/v{vnum}"
    keys = {"video_key": storage.put_file(f"{prefix}/clip.mp4", result.video, "video/mp4"),
            "srt_key": storage.put_file(f"{prefix}/captions.srt", result.srt) if result.srt else None,
            "ass_key": storage.put_file(f"{prefix}/captions.ass", result.ass) if result.ass else None,
            "thumbnail_key": storage.put_file(f"{prefix}/thumbnail.jpg", result.thumbnail, "image/jpeg")
            if result.thumbnail else None}
    shutil.rmtree(work, ignore_errors=True)
    with db.session() as s:
        v = s.get(ClipVersion, ver_id)
        assert v is not None
        for k, val in keys.items():
            setattr(v, k, val)
        v.status, v.duration, v.width, v.height = "rendered", result.duration, result.width, result.height
        v.layout_used, v.edit_summary, v.render_seconds = result.layout, result.edit_summary, round(result.seconds, 1)
        c = s.get(Clip, clip_id)
        assert c is not None
        c.current_version_id = ver_id
        if c.status in ("rendering", "failed"):
            c.status = "rendered"
        cd = s.get(Candidate, candidate_id)
        if cd is not None and cd.status in ("new", "ranked"):
            cd.status = "rendered"
    costs.record("render", quantity=time.time() - t0, unit="seconds", campaign_id=cand.campaign_id,
                 source_id=cand.source_id, clip_id=clip_id, version=vnum, output_seconds=round(result.duration, 1))
    costs.record("storage", quantity=float(storage.size(keys["video_key"])), unit="bytes",
                 campaign_id=cand.campaign_id, clip_id=clip_id)
    audit.record("clip.rendered", "clip", clip_id, version=vnum, layout=result.layout)
    return get_clip(clip_id)


def render_top(campaign: str | int | None = None, source_id: int | None = None, top: int = 5,
               spec: dict[str, Any] | None = None, progress: Progress | None = None) -> list[Clip]:
    """Render the best publishable, not-yet-rendered candidates."""
    pool = [c for c in candidates.list_candidates(campaign, source_id) if c.status in ("new", "ranked")]
    chosen = pool[:top]
    out = []
    for i, c in enumerate(chosen):
        out.append(render_candidate(c.id, spec, progress=(lambda f, m, i=i: progress((i + f) / len(chosen), m))
                                    if progress else None))
    return out


def rerender(clip_id: int, changes: dict[str, Any], progress: Progress | None = None) -> Clip:
    clip = get_clip(clip_id)
    current = next((v for v in clip.versions if v.id == clip.current_version_id), None)
    base = dict(current.spec) if current else {}
    base.update(changes)
    return render_candidate(clip.candidate_id, RenderSpec.model_validate(base), progress)


def create_variant(clip_id: int, platform: str, progress: Progress | None = None) -> Clip:
    """A platform-specific version (aspect ratio and duration limits)."""
    if platform not in PLATFORM_PRESETS:
        raise ValueError(f"unknown platform {platform!r}; known: {sorted(PLATFORM_PRESETS)}")
    preset = PLATFORM_PRESETS[platform]
    clip = get_clip(clip_id)
    cand = candidates.get(clip.candidate_id)
    if cand.end - cand.start > preset["max_duration"]:
        raise ValueError(f"clip is {cand.end - cand.start:.0f}s; {platform} allows {preset['max_duration']}s")
    return rerender(clip_id, {"aspect": preset["aspect"], "platform": platform}, progress)


def get_clip(clip_id: int) -> Clip:
    with db.session() as s:
        c = s.get(Clip, clip_id)
        if c is None:
            raise LookupError(f"no clip {clip_id}")
        _ = c.versions, c.candidate
        return c


def current_version(clip: Clip) -> ClipVersion | None:
    return next((v for v in clip.versions if v.id == clip.current_version_id), None)


def list_clips(campaign: str | int | None = None, status: str | None = None) -> list[Clip]:
    with db.session() as s:
        q = select(Clip).order_by(Clip.id)
        if campaign is not None:
            q = q.where(Clip.campaign_id == campaigns.get(campaign).id)
        if status:
            q = q.where(Clip.status == status)
        items = list(s.scalars(q))
        for c in items:
            _ = c.versions, c.candidate
        return items


def export_clip(clip_id: int, dest: Path) -> dict[str, Any]:
    """Approved clip → folder with video, thumbnail, captions and metadata."""
    clip = get_clip(clip_id)
    if clip.status not in ("approved", "published", "exported"):
        raise PermissionError(f"clip {clip_id} is {clip.status}; only approved clips can be exported")
    v = current_version(clip)
    if v is None or not v.video_key:
        raise RenderError(f"clip {clip_id} has no rendered version")
    storage = get_storage()
    dest.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", (clip.title or f"clip-{clip_id}").lower()).strip("-")[:60] or f"clip-{clip_id}"
    files = {}
    for key, name in ((v.video_key, f"{slug}.mp4"), (v.thumbnail_key, f"{slug}.jpg"), (v.srt_key, f"{slug}.srt"),
                      (v.ass_key, f"{slug}.ass")):
        if key:
            shutil.copyfile(storage.local_path(key), dest / name)
            files[name.rsplit(".", 1)[1]] = str(dest / name)
    meta = clip_dict(clip) | {"version": version_dict(v)}
    (dest / f"{slug}.json").write_text(json.dumps(meta, indent=2, default=str))
    files["json"] = str(dest / f"{slug}.json")
    with db.session() as s:
        c = s.get(Clip, clip_id)
        if c and c.status == "approved":
            c.status = "exported"
    audit.record("clip.exported", "clip", clip_id, dest=str(dest))
    return files


def version_dict(v: ClipVersion) -> dict[str, Any]:
    return {"id": v.id, "version": v.version, "status": v.status, "duration": v.duration, "width": v.width,
            "height": v.height, "layout": v.layout_used, "edit_summary": v.edit_summary, "spec": v.spec,
            "video_key": v.video_key, "thumbnail_key": v.thumbnail_key, "srt_key": v.srt_key, "ass_key": v.ass_key,
            "render_seconds": v.render_seconds, "error": v.error,
            "created_at": v.created_at.isoformat() if v.created_at else None}


def clip_dict(c: Clip, detail: bool = False) -> dict[str, Any]:
    v = current_version(c)
    d = {"id": c.id, "candidate_id": c.candidate_id, "campaign_id": c.campaign_id, "source_id": c.source_id,
         "status": c.status, "title": c.title, "description": c.description, "hashtags": c.hashtags,
         "platform_metadata": c.platform_metadata, "review_notes": c.review_notes,
         "reviewed_at": c.reviewed_at.isoformat() if c.reviewed_at else None, "reviewed_by": c.reviewed_by,
         "current_version": version_dict(v) if v else None, "versions": len(c.versions)}
    if detail:
        d["candidate"] = candidates.to_dict(c.candidate, detail=True)
        d["all_versions"] = [version_dict(x) for x in c.versions]
    return d
