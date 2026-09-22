"""Self-hosted OpenShorts (github.com/mutonby/openshorts) as a second opinion
and a better renderer.

OpenShorts finds its own 3-15 moments and renders each with face tracking,
split-screen layouts and captions. clipper imports those moments as ordinary
candidates, so the same agent judge ranks them against its own picks; when an
OpenShorts moment wins, `framing=openshorts` reuses OpenShorts' render.

OpenShorts needs its own moment-picker model (a Gemini key, or LLM_BASE_URL
pointing at a local Ollama); see docker-compose.yml.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import httpx

from ..clipping import clips, sources
from ..clipping.clips import Candidate
from ..config import settings


def _client() -> httpx.Client:
    s = settings()
    headers = {"Authorization": f"Bearer {s.openshorts_api_key}"} if s.openshorts_api_key else {}
    return httpx.Client(base_url=s.openshorts_url, headers=headers, timeout=httpx.Timeout(600, connect=10))


def _check(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenShorts {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def submit(video: Path, min_seconds: float, max_seconds: float, target_clips: int = 15,
           client: httpx.Client | None = None) -> str:
    c = client or _client()
    slot = _check(c.post("/api/uploads", json={"filename": video.name}))
    with video.open("rb") as fh:
        _check(c.put(f"/api/uploads/{slot['upload_id']}", content=fh))
    job = _check(c.post("/api/process", json={
        "upload_id": slot["upload_id"], "acknowledged": True, "target_clips": target_clips,
        "clip_min_seconds": min_seconds, "clip_max_seconds": max_seconds, "layouts": ["auto"],
    }))
    return job["job_id"]


def wait(job_id: str, poll: float = 10.0, timeout: float = 3 * 3600,
         on_log: Callable[[str], None] | None = None, client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or _client()
    seen, deadline = 0, time.time() + timeout
    while time.time() < deadline:
        st = _check(c.get(f"/api/status/{job_id}"))
        logs = st.get("logs") or []
        for line in logs[seen:]:
            if on_log:
                on_log(line)
        seen = len(logs)
        if st.get("status") == "completed":
            return st.get("result") or {}
        if st.get("status") == "failed":
            raise RuntimeError(f"OpenShorts job {job_id} failed: {logs[-1] if logs else 'no logs'}")
        time.sleep(poll)
    raise TimeoutError(f"OpenShorts job {job_id} still running after {timeout:.0f}s")


def import_result(source_id: int, job_id: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """OpenShorts clips -> clipper candidates (unsnapped: its render uses these exact bounds)."""
    cands, refs = [], []
    for i, clip in enumerate(result.get("clips") or []):
        if clip.get("start") is None or clip.get("end") is None:
            continue
        cands.append(Candidate(
            start=float(clip["start"]), end=float(clip["end"]),
            title=clip.get("video_title_for_youtube_short") or clip.get("title") or f"OpenShorts #{i + 1}",
            hook_text=clip.get("viral_hook_text"), framing="openshorts",
            rationale="Proposed by OpenShorts' moment detector",
        ))
        refs.append(f"{job_id}:{i}:{clip.get('video_url') or ''}")
    return clips.add_candidates(source_id, cands, origin="openshorts", snap_bounds=False, origin_refs=refs)


def run(source_id: int, target_clips: int = 15, on_log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    from .. import campaigns

    src = sources.get(source_id)
    spec = campaigns.spec(src["campaign_id"])
    job_id = submit(Path(src["path"]), spec.requirements.min_duration, spec.requirements.max_duration,
                    target_clips)
    if on_log:
        on_log(f"OpenShorts job {job_id} queued")
    return import_result(source_id, job_id, wait(job_id, on_log=on_log))


def download_clip(origin_ref: str, out: Path, client: httpx.Client | None = None) -> Path:
    _, _, video_url = origin_ref.split(":", 2)
    if not video_url:
        raise RuntimeError(f"OpenShorts clip {origin_ref} has no video_url")
    c = client or _client()
    out.parent.mkdir(parents=True, exist_ok=True)
    with c.stream("GET", video_url) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenShorts download {resp.status_code} for {video_url}")
        with out.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    return out
