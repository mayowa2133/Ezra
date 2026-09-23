"""Publishing service: accounts, OAuth connection, supervised publishing,
scheduling, duplicate protection and post records.

Gates before anything leaves the machine:
  1. the clip is approved by a human (if the campaign requires it; default yes)
  2. publish-stage compliance passes for that platform's copy (hashtags,
     mentions, CTA, platform allow-list, campaign window, posting limits)
  3. no duplicate: one post per (clip version, account, platform, variant)
  4. the caller confirmed (CLI prompt / API `confirm=true` / MCP `confirm`)
Every publish attempt is audited.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select

from .. import audit, campaigns, compliance, db, jobs, metadata, render, secrets, security
from ..config import get_settings
from ..db.models import Clip, IntegrationCredentialMetadata, Post, PublishAccount, utcnow
from ..storage import get_storage
from .base import PostRequest, PublishError, pkce_pair
from .providers import PUBLISHERS, get_publisher

__all__ = [
    "PublishError",
    "account_dict",
    "add_account",
    "cancel_post",
    "connect_finish",
    "connect_start",
    "list_accounts",
    "list_posts",
    "post_dict",
    "publish_clip",
    "run_publish",
]

_http_client: httpx.Client | None = None   # tests inject a MockTransport-backed client


def set_http_client(client: httpx.Client | None) -> None:
    global _http_client
    _http_client = client


def _publisher(provider: str):
    return get_publisher(provider, _http_client)


# --- accounts ----------------------------------------------------------------------------

def add_account(platform: str, provider: str, handle: str, credential_ref: str | None = None,
                tz: str = "UTC", meta: dict[str, Any] | None = None) -> PublishAccount:
    if provider not in PUBLISHERS:
        raise ValueError(f"unknown provider {provider!r}; available: {sorted(PUBLISHERS)}")
    if platform not in PUBLISHERS[provider].platforms:
        raise ValueError(f"{provider} cannot post to {platform}")
    ZoneInfo(tz)  # validates
    with db.session() as s:
        acc = s.scalar(select(PublishAccount).where(PublishAccount.platform == platform,
                                                    PublishAccount.handle == handle))
        if acc is None:
            acc = PublishAccount(platform=platform, provider=provider, handle=handle)
            s.add(acc)
        acc.provider, acc.credential_ref, acc.timezone, acc.meta = provider, credential_ref, tz, meta or {}
        acc.status = "connected"
        s.flush()
        return acc


def list_accounts(platform: str | None = None) -> list[PublishAccount]:
    with db.session() as s:
        q = select(PublishAccount).order_by(PublishAccount.id)
        if platform:
            q = q.where(PublishAccount.platform == platform)
        return list(s.scalars(q))


def account_dict(a: PublishAccount) -> dict[str, Any]:
    return {"id": a.id, "platform": a.platform, "provider": a.provider, "handle": a.handle, "status": a.status,
            "timezone": a.timezone, "has_credential": bool(a.credential_ref and secrets.get(a.credential_ref)),
            "meta": {k: v for k, v in (a.meta or {}).items() if "token" not in k}}


def connect_start(provider: str, redirect_uri: str | None = None) -> dict[str, Any]:
    """Begin OAuth: returns the platform consent URL (state + PKCE stored)."""
    pub = _publisher(provider)
    redirect_uri = redirect_uri or f"{get_settings().public_url}/api/integrations/{provider}/callback"
    verifier, challenge = pkce_pair()
    state = security.new_oauth_state(provider, code_verifier=verifier)
    return {"authorize_url": pub.authorize_url(state, redirect_uri, challenge), "state": state,
            "redirect_uri": redirect_uri}


def connect_finish(provider: str, code: str, state: str, redirect_uri: str | None = None) -> PublishAccount:
    row = security.consume_oauth_state(state, provider)
    pub = _publisher(provider)
    redirect_uri = redirect_uri or f"{get_settings().public_url}/api/integrations/{provider}/callback"
    token = pub.exchange_code(code, redirect_uri, row.code_verifier)
    label = pub.account_label(token)
    ref = f"{provider}:{label}"
    secrets.put(ref, token)
    with db.session() as s:
        cred = s.scalar(select(IntegrationCredentialMetadata).where(
            IntegrationCredentialMetadata.provider == provider,
            IntegrationCredentialMetadata.account_label == label))
        if cred is None:
            cred = IntegrationCredentialMetadata(provider=provider, account_label=label, secret_ref=ref)
            s.add(cred)
        cred.scopes = list(pub.oauth_scopes)
        cred.status = "active"
        if token.get("expires_at"):
            cred.expires_at = datetime.fromtimestamp(token["expires_at"], tz=UTC)
    meta = {k: token[k] for k in ("channel_id", "ig_user_id", "open_id") if token.get(k)}
    acc = add_account(pub.platforms[0], provider, label, credential_ref=ref, meta=meta)
    audit.record("account.connected", "publish_account", acc.id, provider=provider, label=label)
    return acc


# --- publishing -----------------------------------------------------------------------------

def _default_account(platform: str) -> PublishAccount:
    accs = [a for a in list_accounts(platform) if a.status == "connected"]
    real = [a for a in accs if a.provider != "local-export"]
    if real:
        return real[0]
    if accs:
        return accs[0]
    raise PublishError(f"no {platform} account connected; `ezra accounts add` or connect via /integrations")


def posting_counts(campaign_id: int | None, when: datetime, tz: str = "UTC") -> dict[str, Any]:
    local = when.astimezone(ZoneInfo(tz))
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    with db.session() as s:
        q = select(Post.platform, func.count()).where(
            Post.status.in_(("scheduled", "publishing", "published")),
            func.coalesce(Post.scheduled_at, Post.created_at) >= day_start,
            func.coalesce(Post.scheduled_at, Post.created_at) < day_end).group_by(Post.platform)
        if campaign_id is not None:
            q = q.where(Post.campaign_id == campaign_id)
        rows: dict[str, int] = {platform: int(n) for platform, n in s.execute(q).all()}
    return {"day_total": sum(rows.values()), "day_by_platform": rows}


def _features(clip: Clip, platform: str, when: datetime) -> dict[str, Any]:
    """Snapshot of what produced this post, for the performance feedback loop."""
    v = render.current_version(clip)
    cand = clip.candidate
    spec = (v.spec if v else {}) or {}
    import re as _re

    return {
        "clip_id": clip.id, "candidate_id": cand.id, "source_id": clip.source_id, "campaign_id": clip.campaign_id,
        "duration": round(v.duration, 2) if v and v.duration else round(cand.end - cand.start, 2),
        "hook_type": cand.hook_type, "topic": cand.topic, "speaker": (cand.speakers or [None])[0],
        "opening": " ".join(_re.findall(r"[a-z0-9$']+", (cand.transcript or "").lower())[:2]),
        "opening_words": " ".join((cand.transcript or "").split()[:8]),
        "rank_score": cand.rank_score, "content_score": cand.content_score, "confidence": cand.confidence,
        **{k: getattr(cand, f"{k}_score") for k in ("hook", "retention", "context", "emotion", "novelty",
                                                     "discussion", "payoff", "visual", "campaign_fit")},
        "layout": v.layout_used if v else None, "caption_theme": spec.get("caption_theme"),
        "aspect": spec.get("aspect"), "has_cta": bool(spec.get("cta_text")),
        "silence_removed": bool((v.edit_summary or {}).get("removed_seconds")) if v else False,
        "punch_in": spec.get("punch_in"), "variant": spec.get("variant"), "platform": platform,
        "posting_hour": when.hour, "weekday": when.strftime("%a"),
    }


def publish_clip(clip_id: int, platforms: list[str] | None = None, account_ids: dict[str, int] | None = None,
                 visibility: str = "public", schedule_at: datetime | str | None = None, tz: str | None = None,
                 confirm: bool = False, actor: str = "user", experiment_id: int | None = None,
                 variant: str | None = None) -> dict[str, Any]:
    """Validate everything, then (with confirm) create posts and queue the publish jobs.
    Without confirm it is a dry run that returns exactly what would be posted."""
    if visibility not in ("public", "private", "unlisted"):
        raise ValueError("visibility must be public | private | unlisted")
    clip = render.get_clip(clip_id)
    camp = campaigns.get(clip.campaign_id) if clip.campaign_id else None
    platforms = platforms or (camp.allowed_platforms if camp else ["youtube"])
    problems: list[str] = []
    if clip.status not in ("approved", "exported", "published") and (camp is None or camp.human_approval_required):
        problems.append(f"clip {clip_id} is '{clip.status}', not approved by a reviewer")
    if clip.candidate.compliance_status == "FAIL":
        problems.append("clip fails campaign compliance")
    v = render.current_version(clip)
    if v is None or not v.video_key:
        problems.append("clip has no rendered video")
    when = _parse_when(schedule_at, tz) if schedule_at else utcnow()
    meta = clip.platform_metadata or {}
    missing = [p for p in platforms if p not in meta]
    if missing:
        meta = {**meta, **metadata.generate(clip_id, missing, save=True)["metadata"]}
    plans = []
    for p in platforms:
        acc_id = (account_ids or {}).get(p)
        try:
            acc = _get_account(acc_id) if acc_id else _default_account(p)
        except (PublishError, LookupError) as e:
            problems.append(str(e))
            continue
        pub = PUBLISHERS[acc.provider]
        if visibility != "public" and not pub.supports_private and p == "instagram":
            problems.append("instagram: no private posts; leave it out of a private test")
        check = compliance.evaluate(camp, "publish", copy_text=metadata.copy_text(meta, p), platforms=[p],
                                    when=when, posting_counts=posting_counts(camp.id if camp else None, when,
                                                                              acc.timezone))
        if check["status"] == "FAIL":
            problems += [f"{p}: {r['message']}" for r in check["reasons"] if r["outcome"] == "fail"]
        key = f"{v.id if v else 0}:{acc.id}:{p}:{variant or '-'}:{visibility}"
        with db.session() as s:
            dup = s.scalar(select(Post).where(Post.idempotency_key == key))
            if dup is None and not variant and visibility == "public":
                dup = s.scalar(select(Post).where(Post.clip_id == clip_id, Post.account_id == acc.id,
                                                  Post.platform == p, Post.visibility == "public",
                                                  Post.status.in_(("scheduled", "publishing", "published"))))
        if dup is not None:
            problems.append(f"{p}: already posted/scheduled to {acc.handle} (post {dup.id}); duplicate refused")
        plans.append({"platform": p, "account_id": acc.id, "account": acc.handle, "provider": acc.provider,
                      "idempotency_key": key, "copy": meta.get(p), "compliance": check["status"],
                      "review_notes": [r["message"] for r in check["reasons"] if r["outcome"] == "review"]})
    result = {"clip_id": clip_id, "visibility": visibility, "scheduled_at": when.isoformat() if schedule_at else None,
              "posts": plans, "problems": problems, "published": False}
    if problems or not confirm:
        result["dry_run"] = not problems
        return result
    created, job_ids = [], []
    for plan in plans:
        with db.session() as s:
            post = Post(clip_id=clip_id, clip_version_id=v.id if v else None, account_id=plan["account_id"],
                        campaign_id=clip.campaign_id, platform=plan["platform"], provider=plan["provider"],
                        status="scheduled" if schedule_at else "publishing", visibility=visibility,
                        caption=(plan["copy"] or {}).get("caption"), title=(plan["copy"] or {}).get("title"),
                        scheduled_at=when if schedule_at else None, timezone=tz or "UTC",
                        idempotency_key=plan["idempotency_key"], experiment_id=experiment_id,
                        experiment_variant=variant, features=_features(clip, plan["platform"], when))
            s.add(post)
            s.flush()
            pid = post.id
        audit.record("post.created", "post", pid, actor=actor, clip_id=clip_id, platform=plan["platform"],
                     visibility=visibility, scheduled_at=result["scheduled_at"])
        if not schedule_at:
            job_ids.append(jobs.enqueue("publish_post", {"post_id": pid}, dedupe_key=f"publish-{pid}", priority=50).id)
        created.append(pid)
    result.update(published=True, post_ids=created, job_ids=job_ids)
    return result


def _get_account(account_id: int) -> PublishAccount:
    with db.session() as s:
        a = s.get(PublishAccount, account_id)
        if a is None:
            raise LookupError(f"no publish account {account_id}")
        return a


def _parse_when(when: datetime | str, tz: str | None) -> datetime:
    dt = datetime.fromisoformat(when) if isinstance(when, str) else when
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz or "UTC"))
    if dt < utcnow() - timedelta(minutes=1):
        raise ValueError(f"schedule time {dt.isoformat()} is in the past")
    return dt.astimezone(UTC)


def run_publish(post_id: int) -> dict[str, Any]:
    """Executed by the `publish_post` job."""
    with db.session() as s:
        post = s.get(Post, post_id)
        if post is None:
            raise LookupError(f"no post {post_id}")
        if post.status in ("published", "cancelled"):
            return {"post_id": post_id, "status": post.status, "note": "nothing to do"}
        post.status = "publishing"
        acc = s.get(PublishAccount, post.account_id) if post.account_id else None
    if acc is None:
        raise PublishError(f"post {post_id} has no account")
    clip = render.get_clip(post.clip_id)
    v = next((x for x in clip.versions if x.id == post.clip_version_id), None) or render.current_version(clip)
    if v is None or not v.video_key:
        raise PublishError(f"clip {clip.id} has no rendered video")
    st = get_storage()
    meta = (clip.platform_metadata or {}).get(post.platform, {})
    req = PostRequest(video=st.local_path(v.video_key), title=post.title or clip.title,
                      caption=post.caption or meta.get("caption") or clip.title or "",
                      description=meta.get("description"), hashtags=meta.get("hashtags") or clip.hashtags or [],
                      visibility=post.visibility,
                      thumbnail=st.local_path(v.thumbnail_key) if v.thumbnail_key else None)
    pub = _publisher(acc.provider)
    account = account_dict(acc) | {"credential_ref": acc.credential_ref, "meta": acc.meta or {}}
    try:
        res = pub.publish(account, req, post.platform)
    except jobs.RetryableError:
        with db.session() as s:
            p = s.get(Post, post_id)
            p.retries += 1  # type: ignore[union-attr]
        raise
    except Exception as e:
        with db.session() as s:
            p = s.get(Post, post_id)
            p.status, p.error = "failed", f"{type(e).__name__}: {e}"[:2000]  # type: ignore[union-attr]
        audit.record("post.failed", "post", post_id, error=str(e)[:500])
        raise
    with db.session() as s:
        p = s.get(Post, post_id)
        assert p is not None
        p.status = "published" if res.status == "published" else ("scheduled" if res.status == "scheduled"
                                                                   else "publishing")
        p.external_id, p.url, p.raw_response = res.external_id, res.url, res.raw
        p.error = None
        if res.status == "published":
            p.published_at = utcnow()
        c = s.get(Clip, p.clip_id)
        if c is not None and p.visibility == "public" and res.status in ("published", "scheduled"):
            c.status = "published"
    audit.record("post.published" if res.status == "published" else "post.submitted", "post", post_id,
                 provider=acc.provider, external_id=res.external_id, url=res.url)
    return {"post_id": post_id, "status": res.status, "url": res.url, "external_id": res.external_id}


def refresh_statuses() -> list[dict[str, Any]]:
    """Resolve posts still processing on the platform side."""
    out = []
    with db.session() as s:
        pending = list(s.scalars(select(Post).where(Post.status == "publishing", Post.external_id.is_not(None))))
    for post in pending:
        acc = _get_account(post.account_id) if post.account_id else None
        if acc is None:
            continue
        pub = _publisher(acc.provider)
        try:
            res = pub.refresh_status(account_dict(acc) | {"credential_ref": acc.credential_ref, "meta": acc.meta or {}},
                                     post.external_id)
        except PublishError as e:
            with db.session() as s:
                p = s.get(Post, post.id)
                p.status, p.error = "failed", str(e)[:2000]  # type: ignore[union-attr]
            out.append({"post_id": post.id, "status": "failed"})
            continue
        if res and res.status == "published":
            with db.session() as s:
                p = s.get(Post, post.id)
                assert p is not None
                p.status, p.external_id, p.url, p.published_at = "published", res.external_id, res.url, utcnow()
            out.append({"post_id": post.id, "status": "published", "url": res.url})
    return out


def cancel_post(post_id: int, actor: str = "user") -> Post:
    with db.session() as s:
        p = s.get(Post, post_id)
        if p is None:
            raise LookupError(f"no post {post_id}")
        if p.status not in ("scheduled", "failed"):
            raise ValueError(f"post {post_id} is {p.status}; only scheduled or failed posts can be cancelled")
        p.status = "cancelled"
    audit.record("post.cancelled", "post", post_id, actor=actor)
    return get_post(post_id)


def retry_post(post_id: int, actor: str = "user") -> dict[str, Any]:
    with db.session() as s:
        p = s.get(Post, post_id)
        if p is None:
            raise LookupError(f"no post {post_id}")
        if p.status != "failed":
            raise ValueError(f"post {post_id} is {p.status}; only failed posts can be retried")
        p.status, p.error = "publishing", None
    job = jobs.enqueue("publish_post", {"post_id": post_id},
                       dedupe_key=f"publish-{post_id}-retry-{utcnow().timestamp()}")
    audit.record("post.retry", "post", post_id, actor=actor)
    return {"post_id": post_id, "job_id": job.id}


def get_post(post_id: int) -> Post:
    with db.session() as s:
        p = s.get(Post, post_id)
        if p is None:
            raise LookupError(f"no post {post_id}")
        return p


def list_posts(campaign: str | int | None = None, status: str | None = None) -> list[Post]:
    with db.session() as s:
        q = select(Post).order_by(Post.id.desc())
        if campaign is not None:
            q = q.where(Post.campaign_id == campaigns.get(campaign).id)
        if status:
            q = q.where(Post.status == status)
        return list(s.scalars(q))


def post_dict(p: Post) -> dict[str, Any]:
    def iso(d: datetime | None) -> str | None:
        a = db.aware(d)
        return a.isoformat() if a else None

    return {"id": p.id, "clip_id": p.clip_id, "clip_version_id": p.clip_version_id, "account_id": p.account_id,
            "campaign_id": p.campaign_id, "platform": p.platform, "provider": p.provider, "status": p.status,
            "visibility": p.visibility, "external_id": p.external_id, "url": p.url, "title": p.title,
            "caption": p.caption, "scheduled_at": iso(p.scheduled_at), "timezone": p.timezone,
            "published_at": iso(p.published_at), "error": p.error, "retries": p.retries,
            "experiment_id": p.experiment_id, "experiment_variant": p.experiment_variant,
            "actual_payout": p.actual_payout, "created_at": iso(p.created_at)}
