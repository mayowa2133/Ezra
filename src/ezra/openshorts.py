"""Self-hosted OpenShorts (github.com/mutonby/openshorts, MIT) as a second
moment detector.

With EZRA_CLIP_ENGINE=openshorts, candidate generation also sends the source to
an OpenShorts backend (docker compose --profile openshorts up), waits for its
moments, and imports them as ordinary candidates (origin "openshorts"). They go
through the same word-edge snapping, nine-factor scoring, compliance and ranking
as Ezra's own windows and are rendered by Ezra's renderer, so the two detectors
compete on equal terms. OpenShorts needs its own moment-picker model (a Gemini
key, or an OpenAI-compatible local server); Ezra only talks to its REST API.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from . import db, secrets, sources
from .config import get_settings
from .db.models import Candidate

Log = Callable[[str], None]


class OpenShortsError(RuntimeError):
    pass


def client() -> httpx.Client:
    key = secrets.get("openshorts")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.Client(base_url=get_settings().openshorts_url, headers=headers,
                        timeout=httpx.Timeout(600, connect=10))


def _check(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise OpenShortsError(f"OpenShorts {resp.status_code}: {resp.text[:500]}")
    data: dict[str, Any] = resp.json()
    return data


def submit(video: Path, min_seconds: float, max_seconds: float, target_clips: int = 15,
           http: httpx.Client | None = None) -> str:
    c = http or client()
    slot = _check(c.post("/api/uploads", json={"filename": video.name}))
    with video.open("rb") as fh:
        _check(c.put(f"/api/uploads/{slot['upload_id']}", content=fh))
    job = _check(c.post("/api/process", json={
        "upload_id": slot["upload_id"], "acknowledged": True, "target_clips": target_clips,
        "clip_min_seconds": min_seconds, "clip_max_seconds": max_seconds, "layouts": ["auto"],
    }))
    return str(job["job_id"])


def wait(job_id: str, poll: float = 10.0, timeout: float = 3 * 3600, on_log: Log | None = None,
         http: httpx.Client | None = None) -> dict[str, Any]:
    c = http or client()
    seen, deadline = 0, time.time() + timeout
    while time.time() < deadline:
        st = _check(c.get(f"/api/status/{job_id}"))
        logs = st.get("logs") or []
        for line in logs[seen:]:
            if on_log:
                on_log(str(line))
        seen = len(logs)
        if st.get("status") == "completed":
            return st.get("result") or {}
        if st.get("status") == "failed":
            raise OpenShortsError(f"OpenShorts job {job_id} failed: {logs[-1] if logs else 'no logs'}")
        time.sleep(poll)
    raise OpenShortsError(f"OpenShorts job {job_id} still running after {timeout:.0f}s")


def import_result(source_id: int, job_id: str, result: dict[str, Any],
                  campaign: str | int | None = None) -> list[Candidate]:
    """OpenShorts moments → Ezra candidates (snapped, scored, compliance-checked)."""
    from . import candidates

    out: list[Candidate] = []
    for i, clip in enumerate(result.get("clips") or []):
        if clip.get("start") is None or clip.get("end") is None:
            continue
        try:
            cand = candidates.create_custom(
                source_id, float(clip["start"]), float(clip["end"]),
                title=clip.get("video_title_for_youtube_short") or clip.get("title"),
                hook=clip.get("viral_hook_text"), reason="Proposed by OpenShorts' moment detector",
                origin="openshorts", campaign=campaign)
        except ValueError:     # no speech in that range
            continue
        with db.session() as s:
            row = s.get(Candidate, cand.id)
            if row is not None:
                row.origin_ref = f"{job_id}:{i}"
        out.append(cand)
    return out


def run(source_id: int, campaign: str | int | None = None, target_clips: int = 15, on_log: Log | None = None,
        http: httpx.Client | None = None, poll: float = 10.0) -> list[Candidate]:
    from . import campaigns

    src = sources.get(source_id)
    ref = campaign if campaign is not None else src.campaign_id
    camp = campaigns.get(ref) if ref is not None else None
    lo, hi = (camp.min_duration, camp.max_duration) if camp else (15.0, 60.0)
    job_id = submit(sources.local_path(src), lo, hi, target_clips, http=http)
    if on_log:
        on_log(f"OpenShorts job {job_id} queued")
    return import_result(source_id, job_id, wait(job_id, poll=poll, on_log=on_log, http=http), campaign)
