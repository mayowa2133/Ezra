"""Ezra HTTP API (FastAPI). The dashboard, CLI and MCP server call the same
application services this module exposes.

Auth: when EZRA_API_TOKEN is set every /api route except /api/health and the
OAuth callback requires `Authorization: Bearer <token>`. Media URLs handed to
the browser are HMAC-signed and short-lived instead (a <video> tag cannot send
headers). Long work is queued as jobs; responses return the job to poll.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import (
    analysis,
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
    security,
    sources,
    transcription,
)
from .. import analytics as perf
from ..config import get_settings
from ..storage import StorageError, get_storage


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.migrate()
    if os.environ.get("EZRA_EMBEDDED_WORKER") == "1":
        start_embedded_worker()
    yield


app = FastAPI(title="Ezra API", version="0.1.0", lifespan=lifespan,
              description="Agent-native short-form clipping and performance optimization")
_limiter: security.RateLimiter | None = None


# --- middleware ---------------------------------------------------------------------------

@app.middleware("http")
async def guard(request: Request, call_next):
    global _limiter
    s = get_settings()
    if _limiter is None:
        _limiter = security.RateLimiter(s.rate_limit_per_minute)
    ip = request.client.host if request.client else "unknown"
    if request.url.path.startswith("/api") and not _limiter.allow(ip):
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _cors() -> None:
    s = get_settings()
    origins = {s.web_url.rstrip("/"), "http://localhost:3000", "http://127.0.0.1:3000"}
    app.add_middleware(CORSMiddleware, allow_origins=sorted(origins), allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])


_cors()


def require_token(request: Request) -> str:
    token = get_settings().api_token
    if not token:
        return "local"
    header = request.headers.get("authorization", "")
    if not hmac.compare_digest(header, f"Bearer {token}"):
        raise HTTPException(401, "missing or invalid API token")
    return "api"


Auth = Depends(require_token)


@app.exception_handler(LookupError)
async def _404(_: Request, e: LookupError) -> JSONResponse:
    return JSONResponse({"detail": str(e).strip("'\"")}, status_code=404)


@app.exception_handler(PermissionError)
async def _403(_: Request, e: PermissionError) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=403)


@app.exception_handler(ValueError)
async def _400(_: Request, e: ValueError) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(StorageError)
async def _storage(_: Request, e: StorageError) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=404)


@app.exception_handler(secrets.SecretError)
async def _secret(_: Request, e: secrets.SecretError) -> JSONResponse:
    return JSONResponse({"detail": str(e), "missing_credential": True}, status_code=424)


@app.exception_handler(publishing.PublishError)
async def _publish(_: Request, e: publishing.PublishError) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=502)


# --- media signing -------------------------------------------------------------------------

def _sign(key: str, exp: int) -> str:
    secret = (get_settings().secret_key or get_settings().api_token or "ezra-local").encode()
    return hmac.new(secret, f"{key}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def media_url(key: str | None, ttl: int = 6 * 3600) -> str | None:
    if not key:
        return None
    exp = int(time.time()) + ttl
    return f"{get_settings().public_url}/api/media?key={key}&exp={exp}&sig={_sign(key, exp)}"


@app.get("/api/media")
def media(request: Request, key: str, exp: int, sig: str) -> Response:
    if exp < time.time() or not hmac.compare_digest(sig, _sign(key, exp)):
        raise HTTPException(403, "media link expired or invalid")
    path = get_storage().local_path(key)
    return _ranged(path, request)


def _ranged(path: Path, request: Request) -> Response:
    """HTTP range support so the dashboard player can seek."""
    size = path.stat().st_size
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    rng = request.headers.get("range")
    if not rng:
        return FileResponse(path, media_type=ctype, headers={"Accept-Ranges": "bytes"})
    start_s, _, end_s = rng.replace("bytes=", "").partition("-")
    start = int(start_s or 0)
    end = min(int(end_s) if end_s else size - 1, size - 1)
    if start > end or start >= size:
        raise HTTPException(416, "range not satisfiable")

    def body():
        with open(path, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=206, media_type=ctype, headers={
        "Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1)})


# --- health & dashboard ------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    db.migrate()
    return {"ok": True, "version": app.version, "storage": get_settings().storage,
            "database": get_settings().db_url.split(":", 1)[0]}


@app.get("/api/dashboard", dependencies=[Auth])
def dashboard() -> dict[str, Any]:
    camps = campaigns.list_campaigns()
    return {
        "campaigns": [{"slug": c.slug, "name": c.name, "cpm": c.cpm} for c in camps],
        "sources": len(sources.list_sources()),
        "candidates": len(candidates.list_candidates(include_failed=True)),
        "review_pending": len(review.queue()),
        "clips": {st: len(render.list_clips(status=st)) for st in
                  ("rendered", "approved", "rejected", "published", "exported", "failed")},
        "posts": {st: len(publishing.list_posts(status=st)) for st in
                  ("scheduled", "publishing", "published", "failed")},
        "jobs": [jobs.as_dict(j) for j in jobs.list_jobs(limit=8)],
        "earnings": [economics.earnings(c.slug) | {"posts": None} for c in camps],
        "costs": costs.summary(),
    }


# --- campaigns -----------------------------------------------------------------------------

class ImportBody(BaseModel):
    text: str
    format: str | None = Field(None, description="yaml | json | csv (sniffed when omitted)")


@app.get("/api/campaigns", dependencies=[Auth])
def list_campaigns() -> list[dict[str, Any]]:
    return [campaigns.to_dict(c) for c in campaigns.list_campaigns()]


@app.post("/api/campaigns", dependencies=[Auth], status_code=201)
def create_campaign(spec: campaigns.CampaignSpec) -> dict[str, Any]:
    return campaigns.to_dict(campaigns.upsert(spec))


@app.post("/api/campaigns/import", dependencies=[Auth], status_code=201)
def import_campaigns(body: ImportBody) -> list[dict[str, Any]]:
    return [campaigns.to_dict(c) for c in campaigns.import_text(body.text, body.format, ingest_sources=False)]


@app.get("/api/campaigns/{ref}", dependencies=[Auth])
def get_campaign(ref: str) -> dict[str, Any]:
    return campaigns.to_dict(campaigns.get(ref))


class RuleBody(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    severity: str = "fail"
    description: str = ""


@app.post("/api/campaigns/{ref}/rules", dependencies=[Auth], status_code=201)
def add_rule(ref: str, body: RuleBody) -> dict[str, Any]:
    r = campaigns.add_rule(ref, body.kind, body.params, body.severity, body.description)
    return {"id": r.id, "kind": r.kind, "severity": r.severity}


@app.get("/api/campaigns/{ref}/earnings", dependencies=[Auth])
def earnings(ref: str) -> dict[str, Any]:
    return economics.earnings(ref)


@app.get("/api/campaigns/{ref}/report", dependencies=[Auth])
def campaign_report(ref: str) -> dict[str, Any]:
    c = campaigns.get(ref)
    return {"campaign": campaigns.to_dict(c), "earnings": economics.earnings(ref),
            "insights": perf.insights(c.id), "review_pending": len(review.queue(ref)),
            "posts": [publishing.post_dict(p) for p in publishing.list_posts(ref)]}


@app.get("/api/campaigns/{ref}/insights", dependencies=[Auth])
def campaign_insights(ref: str) -> dict[str, Any]:
    return perf.insights(campaigns.get(ref).id)


@app.post("/api/campaigns/{ref}/optimize", dependencies=[Auth])
def optimize(ref: str, apply: bool = False) -> dict[str, Any]:
    return runner.optimize(ref, apply)


class RevenueBody(BaseModel):
    amount: float
    post_id: int | None = None
    notes: str | None = None


@app.post("/api/campaigns/{ref}/revenue", dependencies=[Auth], status_code=201)
def add_revenue(ref: str, body: RevenueBody) -> dict[str, Any]:
    r = economics.record_revenue(ref, body.amount, body.post_id, notes=body.notes)
    return {"id": r.id, "amount": r.amount}


class RunBody(BaseModel):
    render_top: int = 5
    max_candidates: int = 40
    autonomous: bool = False
    spec: dict[str, Any] | None = None


@app.post("/api/campaigns/{ref}/run", dependencies=[Auth], status_code=202)
def run_campaign(ref: str, body: RunBody) -> dict[str, Any]:
    c = campaigns.get(ref)
    return jobs.as_dict(jobs.enqueue("run_campaign", {"campaign": c.slug, **body.model_dump()},
                                     dedupe_key=f"run-{c.slug}"))


# --- sources -------------------------------------------------------------------------------

@app.get("/api/sources", dependencies=[Auth])
def list_sources(campaign: str | None = None) -> list[dict[str, Any]]:
    return [sources.to_dict(s) for s in sources.list_sources(campaign)]


@app.post("/api/sources", dependencies=[Auth], status_code=201)
def upload_source(file: UploadFile = File(...), campaign: str | None = Form(None),
                        rights_basis: str = Form("unknown"), title: str | None = Form(None)) -> dict[str, Any]:
    name = Path(file.filename or "upload.mp4").name
    if Path(name).suffix.lower() not in security.VIDEO_EXTENSIONS | security.AUDIO_EXTENSIONS:
        raise HTTPException(415, f"unsupported file type {Path(name).suffix!r}")
    limit = get_settings().max_upload_mb * 1024 * 1024
    tmpdir = Path(tempfile.mkdtemp(prefix="ezra-upload-", dir=get_settings().work_dir))
    dest = tmpdir / name
    written = 0
    try:
        with open(dest, "wb") as out:
            while chunk := file.file.read(1 << 20):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f"file exceeds {get_settings().max_upload_mb} MB")
                out.write(chunk)
        src = sources.ingest(dest, campaign=campaign, title=title, rights_basis=rights_basis, actor="api")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return sources.to_dict(src)


class ImportPathBody(BaseModel):
    path: str
    campaign: str | None = None
    rights_basis: str = "unknown"
    title: str | None = None


@app.post("/api/sources/import", dependencies=[Auth], status_code=201)
def import_source(body: ImportPathBody) -> dict[str, Any]:
    path = security.safe_import_path(body.path)
    return sources.to_dict(sources.ingest(path, body.campaign, body.title, body.rights_basis, actor="api"))


@app.get("/api/sources/{source_id}", dependencies=[Auth])
def get_source(source_id: int) -> dict[str, Any]:
    src = sources.get(source_id)
    tr = transcription.latest(source_id)
    return sources.to_dict(src) | {"analysis": analysis.summary_for(source_id),
                                   "transcript": transcription.to_dict(tr) if tr else None,
                                   "media_url": media_url(src.storage_key)}


class RightsBody(BaseModel):
    status: str
    basis: str | None = None
    notes: str | None = None


@app.post("/api/sources/{source_id}/rights", dependencies=[Auth])
def set_rights(source_id: int, body: RightsBody) -> dict[str, Any]:
    return sources.to_dict(sources.set_rights(source_id, body.status, body.basis, body.notes, actor="api"))


@app.post("/api/sources/{source_id}/analyze", dependencies=[Auth], status_code=202)
def analyze(source_id: int, force: bool = False) -> dict[str, Any]:
    sources.get(source_id)
    return jobs.as_dict(jobs.enqueue("analyze_source", {"source_id": source_id, "force": force},
                                     dedupe_key=f"analyze-{source_id}"))


@app.get("/api/sources/{source_id}/analysis", dependencies=[Auth])
def get_analysis(source_id: int) -> dict[str, Any]:
    return {k: {"provider": v.provider, "version": v.version, "confidence": v.confidence, "data": v.data}
            for k, v in analysis.all_for(source_id).items()}


@app.get("/api/sources/{source_id}/transcript", dependencies=[Auth])
def get_transcript(source_id: int, start: float = 0.0, max_chars: int = 30000) -> dict[str, Any]:
    return transcription.page(source_id, start, max_chars)


class FindBody(BaseModel):
    campaign: str | None = None
    max_candidates: int = 40


@app.post("/api/sources/{source_id}/candidates", dependencies=[Auth], status_code=202)
def find(source_id: int, body: FindBody) -> dict[str, Any]:
    sources.get(source_id)
    return jobs.as_dict(jobs.enqueue("find_candidates", {"source_id": source_id, **body.model_dump()},
                                     dedupe_key=f"find-{source_id}"))


# --- candidates ------------------------------------------------------------------------------

@app.get("/api/candidates", dependencies=[Auth])
def list_candidates(campaign: str | None = None, source_id: int | None = None, top: int | None = None,
                    include_failed: bool = False) -> list[dict[str, Any]]:
    return [candidates.to_dict(c) for c in candidates.list_candidates(campaign, source_id, top, include_failed)]


@app.get("/api/candidates/{candidate_id}", dependencies=[Auth])
def get_candidate(candidate_id: int) -> dict[str, Any]:
    return candidates.to_dict(candidates.get(candidate_id), detail=True)


class CustomCandidate(BaseModel):
    source_id: int
    start: float
    end: float
    title: str | None = None
    hook: str | None = None
    hook_type: str | None = None
    reason: str | None = None


@app.post("/api/candidates", dependencies=[Auth], status_code=201)
def create_candidate(body: CustomCandidate) -> dict[str, Any]:
    c = candidates.create_custom(body.source_id, body.start, body.end, body.title, body.hook, body.hook_type,
                                 body.reason, origin="manual")
    return candidates.to_dict(c, detail=True)


class ScoreBody(BaseModel):
    scores: dict[str, float]
    notes: str | None = None
    hook: str | None = None
    title: str | None = None
    hook_type: str | None = None
    compliant: bool = True
    compliance_notes: str | None = None


@app.post("/api/candidates/{candidate_id}/scores", dependencies=[Auth])
def score_candidate(candidate_id: int, body: ScoreBody) -> dict[str, Any]:
    return candidates.to_dict(candidates.set_agent_scores(candidate_id, **body.model_dump()), detail=True)


class RankBody(BaseModel):
    campaign: str | None = None
    source_id: int | None = None
    use_model: bool = True


@app.post("/api/candidates/rank", dependencies=[Auth], status_code=202)
def rank(body: RankBody) -> dict[str, Any]:
    return jobs.as_dict(jobs.enqueue("rank_candidates", body.model_dump()))


class RenderBody(BaseModel):
    spec: dict[str, Any] | None = None


@app.post("/api/candidates/{candidate_id}/render", dependencies=[Auth], status_code=202)
def render_candidate(candidate_id: int, body: RenderBody) -> dict[str, Any]:
    c = candidates.get(candidate_id)
    if c.compliance_status == "FAIL":
        raise PermissionError(f"candidate {candidate_id} fails compliance and cannot be rendered")
    if body.spec:
        render.RenderSpec.model_validate(body.spec)
    return jobs.as_dict(jobs.enqueue("render_candidate", {"candidate_id": candidate_id, "spec": body.spec}))


class RenderTopBody(BaseModel):
    campaign: str | None = None
    source_id: int | None = None
    top: int = 5
    spec: dict[str, Any] | None = None


@app.post("/api/render/top", dependencies=[Auth], status_code=202)
def render_top(body: RenderTopBody) -> dict[str, Any]:
    return jobs.as_dict(jobs.enqueue("render_top", body.model_dump()))


# --- clips & review --------------------------------------------------------------------------

def _clip_out(c: Any, detail: bool = False) -> dict[str, Any]:
    d = render.clip_dict(c, detail)
    v = d.get("current_version") or {}
    d["video_url"] = media_url(v.get("video_key"))
    d["thumbnail_url"] = media_url(v.get("thumbnail_key"))
    d["captions_url"] = media_url(v.get("srt_key"))
    return d


@app.get("/api/clips", dependencies=[Auth])
def list_clips(campaign: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    return [_clip_out(c) for c in render.list_clips(campaign, status)]


@app.get("/api/clips/{clip_id}", dependencies=[Auth])
def get_clip(clip_id: int) -> dict[str, Any]:
    return _clip_out(render.get_clip(clip_id), detail=True)


@app.get("/api/review", dependencies=[Auth])
def review_queue(campaign: str | None = None, include_decided: bool = False) -> list[dict[str, Any]]:
    out = []
    for card in review.queue(campaign, include_decided):
        card["video_url"] = media_url(card["video_key"])
        card["thumbnail_url"] = media_url(card["thumbnail_key"])
        out.append(card)
    return out


class ReviewBody(BaseModel):
    notes: str | None = None
    reviewer: str = "reviewer"


@app.post("/api/clips/{clip_id}/approve", dependencies=[Auth])
def approve(clip_id: int, body: ReviewBody) -> dict[str, Any]:
    return _clip_out(review.approve(clip_id, actor=body.reviewer, notes=body.notes))


@app.post("/api/clips/{clip_id}/reject", dependencies=[Auth])
def reject(clip_id: int, body: ReviewBody) -> dict[str, Any]:
    return _clip_out(review.reject(clip_id, actor=body.reviewer, reason=body.notes))


class ClipUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None
    platform_metadata: dict[str, Any] | None = None


@app.patch("/api/clips/{clip_id}", dependencies=[Auth])
def update_clip(clip_id: int, body: ClipUpdate) -> dict[str, Any]:
    return _clip_out(review.update(clip_id, **body.model_dump(), actor="api"))


class MetadataBody(BaseModel):
    platforms: list[str] | None = None
    use_model: bool = True


@app.post("/api/clips/{clip_id}/metadata", dependencies=[Auth])
def gen_metadata(clip_id: int, body: MetadataBody) -> dict[str, Any]:
    return metadata.generate(clip_id, body.platforms, body.use_model)


class RerenderBody(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/clips/{clip_id}/rerender", dependencies=[Auth], status_code=202)
def rerender(clip_id: int, body: RerenderBody) -> dict[str, Any]:
    render.get_clip(clip_id)
    return jobs.as_dict(jobs.enqueue("rerender_clip", {"clip_id": clip_id, "changes": body.changes}))


@app.post("/api/clips/{clip_id}/variant/{platform}", dependencies=[Auth], status_code=202)
def variant(clip_id: int, platform: str) -> dict[str, Any]:
    return jobs.as_dict(jobs.enqueue("platform_variant", {"clip_id": clip_id, "platform": platform}))


@app.post("/api/clips/{clip_id}/broll", dependencies=[Auth], status_code=202)
def broll(clip_id: int, provider: str = "textcard", max_inserts: int = 2) -> dict[str, Any]:
    return jobs.as_dict(jobs.enqueue("attach_broll", {"clip_id": clip_id, "provider": provider,
                                                      "max_inserts": max_inserts}))


@app.post("/api/clips/{clip_id}/export", dependencies=[Auth])
def export(clip_id: int) -> dict[str, Any]:
    dest = get_settings().home / "exports" / f"clip-{clip_id}"
    return render.export_clip(clip_id, dest)


# --- publishing ----------------------------------------------------------------------------

class PublishBody(BaseModel):
    platforms: list[str] | None = None
    account_ids: dict[str, int] | None = None
    visibility: str = "public"
    schedule_at: str | None = None
    timezone: str | None = None
    confirm: bool = False
    experiment_id: int | None = None
    variant: str | None = None


@app.post("/api/clips/{clip_id}/publish", dependencies=[Auth])
def publish(clip_id: int, body: PublishBody) -> dict[str, Any]:
    return publishing.publish_clip(clip_id, body.platforms, body.account_ids, body.visibility, body.schedule_at,
                                   body.timezone, body.confirm, actor="api", experiment_id=body.experiment_id,
                                   variant=body.variant)


def _latest(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    return history[-1] if history else None


@app.get("/api/posts", dependencies=[Auth])
def list_posts(campaign: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    out = []
    for p in publishing.list_posts(campaign, status):
        d = publishing.post_dict(p)
        d["metrics"] = _latest(metrics.history(p.id))
        out.append(d)
    return out


@app.post("/api/posts/{post_id}/cancel", dependencies=[Auth])
def cancel_post(post_id: int) -> dict[str, Any]:
    return publishing.post_dict(publishing.cancel_post(post_id, actor="api"))


@app.post("/api/posts/{post_id}/retry", dependencies=[Auth])
def retry_post(post_id: int) -> dict[str, Any]:
    return publishing.retry_post(post_id, actor="api")


class MetricBody(BaseModel):
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    impressions: int | None = None
    avg_watch_seconds: float | None = None
    completion_rate: float | None = None
    followers_gained: int | None = None
    actual_payout: float | None = None


@app.post("/api/posts/{post_id}/metrics", dependencies=[Auth], status_code=201)
def add_metrics(post_id: int, body: MetricBody) -> dict[str, Any]:
    vals = {k: v for k, v in body.model_dump().items() if v is not None and k != "actual_payout"}
    snap = metrics.record(post_id, provider="manual", **vals)
    if body.actual_payout is not None:
        p = publishing.get_post(post_id)
        economics.record_revenue(p.campaign_id, body.actual_payout, post_id)  # type: ignore[arg-type]
    return metrics.snapshot_dict(snap)


@app.get("/api/posts/{post_id}/metrics", dependencies=[Auth])
def post_metrics(post_id: int) -> list[dict[str, Any]]:
    return metrics.history(post_id)


class CSVBody(BaseModel):
    text: str


@app.post("/api/metrics/import", dependencies=[Auth])
def import_metrics(body: CSVBody) -> dict[str, Any]:
    return metrics.import_csv(body.text)


@app.post("/api/metrics/sync", dependencies=[Auth], status_code=202)
def sync_metrics(campaign: str | None = None) -> dict[str, Any]:
    cid = campaigns.get(campaign).id if campaign else None
    return jobs.as_dict(jobs.enqueue("sync_metrics", {"campaign_id": cid}, dedupe_key="sync-metrics"))


# --- accounts & integrations ------------------------------------------------------------------

class AccountBody(BaseModel):
    platform: str
    provider: str
    handle: str
    credential_ref: str | None = None
    timezone: str = "UTC"
    meta: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/accounts", dependencies=[Auth])
def accounts() -> list[dict[str, Any]]:
    return [publishing.account_dict(a) for a in publishing.list_accounts()]


@app.post("/api/accounts", dependencies=[Auth], status_code=201)
def add_account(body: AccountBody) -> dict[str, Any]:
    return publishing.account_dict(publishing.add_account(body.platform, body.provider, body.handle,
                                                          body.credential_ref, body.timezone, body.meta))


@app.get("/api/integrations", dependencies=[Auth])
def integrations() -> dict[str, Any]:
    from ..publishing.providers import PUBLISHERS

    have = set(secrets.refs())
    return {"publishers": [{"provider": n, "platforms": list(p.platforms),
                            "oauth": p.client_secret_ref is not None,
                            "configured": p.client_secret_ref is None or p.client_secret_ref in have}
                           for n, p in PUBLISHERS.items()],
            "secrets_present": sorted(have), "accounts": accounts(),
            "llm": get_settings().llm, "transcriber": get_settings().transcriber,
            "diarizer": get_settings().diarizer, "face_detector": get_settings().face_detector}


@app.get("/api/integrations/{provider}/connect", dependencies=[Auth])
def connect(provider: str) -> dict[str, Any]:
    return publishing.connect_start(provider)


@app.get("/api/integrations/{provider}/callback")
def callback(provider: str, code: str | None = None, state: str | None = None,
             error: str | None = None) -> RedirectResponse:
    """OAuth redirect target: state is single-use and provider-bound."""
    if error or not code or not state:
        return RedirectResponse(security.safe_redirect(f"/integrations?error={error or 'missing_code'}"))
    acc = publishing.connect_finish(provider, code, state)
    return RedirectResponse(security.safe_redirect(f"/integrations?connected={acc.id}"))


# --- experiments, brand kits, jobs, live, settings -----------------------------------------

class ExperimentBody(BaseModel):
    campaign: str | None = None
    name: str
    factor: str
    values: list[Any]
    hypothesis: str | None = None
    min_samples: int = 5


@app.get("/api/experiments", dependencies=[Auth])
def list_experiments(campaign: str | None = None) -> list[dict[str, Any]]:
    return [experiments.to_dict(e) for e in experiments.list_experiments(campaign)]


@app.post("/api/experiments", dependencies=[Auth], status_code=201)
def create_experiment(body: ExperimentBody) -> dict[str, Any]:
    return experiments.to_dict(experiments.create(body.campaign, body.name, body.factor, body.values,
                                                  body.hypothesis, body.min_samples))


@app.post("/api/experiments/{experiment_id}/assign/{clip_id}", dependencies=[Auth])
def assign(experiment_id: int, clip_id: int) -> dict[str, Any]:
    return experiments.assign(experiment_id, clip_id)


@app.get("/api/experiments/{experiment_id}/analysis", dependencies=[Auth])
def experiment_analysis(experiment_id: int) -> dict[str, Any]:
    return experiments.analyze(experiment_id)


@app.get("/api/brand-kits", dependencies=[Auth])
def list_kits() -> list[dict[str, Any]]:
    return [brandkits.to_dict(k) for k in brandkits.list_kits()]


@app.post("/api/brand-kits", dependencies=[Auth], status_code=201)
def create_kit(spec: brandkits.BrandKitSpec) -> dict[str, Any]:
    return brandkits.to_dict(brandkits.upsert(spec))


@app.get("/api/jobs", dependencies=[Auth])
def list_jobs(status: str | None = None, kind: str | None = None,
              limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
    return [jobs.as_dict(j) for j in jobs.list_jobs(status, kind, limit)]


@app.get("/api/jobs/{job_id}", dependencies=[Auth])
def get_job(job_id: int, logs: bool = True) -> dict[str, Any]:
    return jobs.as_dict(jobs.get(job_id), with_logs=logs)


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Auth])
def cancel_job(job_id: int) -> dict[str, Any]:
    return jobs.as_dict(jobs.cancel(job_id))


@app.post("/api/jobs/{job_id}/retry", dependencies=[Auth])
def retry_job(job_id: int) -> dict[str, Any]:
    return jobs.as_dict(jobs.retry(job_id))


class LiveBody(BaseModel):
    campaign: str
    kind: str = "file"
    target: str
    speed: float = 1.0
    chunk_seconds: int = 30
    window_seconds: int = 120
    max_seconds: float | None = None
    render_top: int = 0


@app.post("/api/live", dependencies=[Auth], status_code=202)
def live(body: LiveBody) -> dict[str, Any]:
    if body.kind == "file":
        security.safe_import_path(body.target)
    campaigns.get(body.campaign)
    return jobs.as_dict(jobs.enqueue("live_session", body.model_dump(), max_attempts=1))


@app.get("/api/settings", dependencies=[Auth])
def settings() -> dict[str, Any]:
    s = get_settings()
    return {"storage": s.storage, "database": s.db_url.split(":", 1)[0], "transcriber": s.transcriber,
            "whisper_model": s.whisper_model, "diarizer": s.diarizer, "face_detector": s.face_detector,
            "llm": s.llm, "clip_engine": s.clip_engine, "max_upload_mb": s.max_upload_mb,
            "api_auth": bool(s.api_token), "secrets_present": secrets.refs(),
            "autonomous_allowed": os.environ.get("EZRA_ALLOW_AUTONOMOUS") == "1"}


@app.get("/api/audit", dependencies=[Auth])
def audit_log(limit: int = 100) -> list[dict[str, Any]]:
    return [{"id": e.id, "at": db.aware(e.at).isoformat(), "actor": e.actor, "action": e.action,  # type: ignore[union-attr]
             "entity_type": e.entity_type, "entity_id": e.entity_id, "details": e.details}
            for e in audit.recent(limit)]


# --- embedded worker (single-process local mode) -----------------------------------------------

def start_embedded_worker() -> threading.Thread:
    from .. import worker

    t = threading.Thread(target=worker.run_forever, name="ezra-embedded-worker", daemon=True)
    t.start()
    return t


