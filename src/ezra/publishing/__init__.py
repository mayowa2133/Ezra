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

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, func, or_, select

from .. import audit, campaigns, compliance, db, jobs, metadata, render, secrets, security
from ..config import get_settings
from ..db.models import Clip, IntegrationCredentialMetadata, Post, PublishAccount, utcnow
from ..storage import get_storage
from .base import PostRequest, PublishError, pkce_pair
from .providers import PUBLISHERS, get_publisher
from .youtube import YouTubePublisher

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


def _youtube() -> YouTubePublisher:
    pub = _publisher("youtube")
    assert isinstance(pub, YouTubePublisher)
    return pub


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
        # updating an account merges: an omitted credential or meta key keeps what OAuth stored
        acc.provider, acc.timezone = provider, tz
        acc.credential_ref = credential_ref or acc.credential_ref
        acc.meta = {**(acc.meta or {}), **(meta or {})}
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
        cred.scopes = list(token.get("scopes") or pub.oauth_scopes)     # what was granted, when known
        cred.status = "active"
        if token.get("expires_at"):
            cred.expires_at = datetime.fromtimestamp(token["expires_at"], tz=UTC)
    meta = {k: token[k] for k in ("channel_id", "ig_user_id", "open_id", "scopes") if token.get(k)}
    acc = add_account(pub.platforms[0], provider, label, credential_ref=ref, meta=meta)
    audit.record("account.connected", "publish_account", acc.id, provider=provider, label=label)
    return acc


# --- publishing -----------------------------------------------------------------------------

def _default_account(platform: str) -> PublishAccount:
    # an account that needs reconnecting is still chosen, so the dry run can say so plainly
    accs = [a for a in list_accounts(platform) if a.status in ("connected", "reconnect_required")]
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
        "caption_emoji": bool(spec.get("caption_emoji")), "creator": _creator(clip.campaign_id),
        "face_rate": (cand.features or {}).get("face_rate"),
        "scene_cuts_per_min": _per_min((cand.features or {}).get("scene_cuts"), cand.end - cand.start),
        "title_style": title_style(clip.title or cand.title or ""),
        "source_captions_hidden": bool((v.edit_summary or {}).get("source_captions")) if v else False,
    }


def _creator(campaign_id: int | None) -> str | None:
    return campaigns.get(campaign_id).creator if campaign_id else None


def _per_min(n: Any, seconds: float) -> float | None:
    return round(float(n) * 60 / seconds, 1) if n is not None and seconds > 0 else None


def title_style(title: str) -> str:
    """Coarse shape of a title, for learning which kinds work: question, quote, number, statement."""
    t = title.strip()
    if t.endswith("?"):
        return "question"
    if t[:1] in "\"'“" or '"' in t or "“" in t:
        return "quote"
    if any(ch.isdigit() for ch in t[:25]) or "$" in t:
        return "number"
    return "statement"


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
        if acc.provider == "youtube" and (visibility == "public" or schedule_at) \
                and not get_settings().youtube_public_allowed:
            problems.append("youtube: public and scheduled uploads need a Google API project that passed YouTube's "
                            "audit (EZRA_YOUTUBE_PUBLIC_ALLOWED=1); until then upload with visibility=private")
        if acc.status == "reconnect_required":
            problems.append(f"{p}: account {acc.handle} needs to be reconnected (ezra accounts connect {acc.provider})")
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
        # platforms that schedule themselves (YouTube publishAt) get the upload now; others are
        # posted by the scheduler when due
        if not schedule_at or PUBLISHERS[plan["provider"]].supports_scheduling:
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


def run_publish(post_id: int, final_attempt: bool = True,
                progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    """Executed by the `publish_post` job. Resumable state (the upload session) is saved on the post
    as it changes, so a retry resumes instead of uploading again, and a post that already has a
    platform id is never uploaded twice."""
    with db.session() as s:
        post = s.get(Post, post_id)
        if post is None:
            raise LookupError(f"no post {post_id}")
        if post.status in ("published", "cancelled", "deleted"):
            return {"post_id": post_id, "status": post.status, "note": "nothing to do"}
        if post.external_id and post.provider != "local-export" and post.status == "scheduled" \
                and post.platform_state:
            return {"post_id": post_id, "status": post.status, "note": "already uploaded; the platform publishes it"}
        post.platform_state = post.platform_state or "uploading"
        if post.status != "scheduled":
            post.status = "publishing"
        acc = s.get(PublishAccount, post.account_id) if post.account_id else None
        upload_state = dict(post.upload_state or {})
    if acc is None:
        raise PublishError(f"post {post_id} has no account", "no_account")
    clip = render.get_clip(post.clip_id)
    v = next((x for x in clip.versions if x.id == post.clip_version_id), None) or render.current_version(clip)
    if v is None or not v.video_key:
        raise PublishError(f"clip {clip.id} has no rendered video", "no_video")
    st = get_storage()
    meta = (clip.platform_metadata or {}).get(post.platform, {})
    pub = _publisher(acc.provider)

    def save_state(state: dict[str, Any]) -> None:
        with db.session() as s2:
            p2 = s2.get(Post, post_id)
            if p2 is not None:
                p2.upload_state = state

    def on_progress(sent: int, total: int) -> None:
        if progress:
            progress(sent / total if total else 1.0, f"uploaded {sent / 1e6:.1f} of {total / 1e6:.1f} MB")

    req = PostRequest(video=st.local_path(v.video_key), title=post.title or meta.get("title") or clip.title,
                      caption=post.caption or meta.get("caption") or clip.title or "",
                      description=meta.get("description"), hashtags=meta.get("hashtags") or clip.hashtags or [],
                      visibility=post.visibility,
                      scheduled_at=db.aware(post.scheduled_at) if pub.supports_scheduling else None,
                      thumbnail=st.local_path(v.thumbnail_key) if v.thumbnail_key else None,
                      upload_state=upload_state, on_state=save_state, on_progress=on_progress)
    account = account_dict(acc) | {"credential_ref": acc.credential_ref, "meta": acc.meta or {}}
    audit.record("post.upload_started", "post", post_id, provider=acc.provider, resumed=bool(upload_state))
    try:
        res = pub.publish(account, req, post.platform)
    except jobs.RetryableError as e:
        code = getattr(e, "code", "transient")
        with db.session() as s:
            p = s.get(Post, post_id)
            assert p is not None
            p.retries += 1
            p.error_code, p.error = code, f"{type(e).__name__}: {e}"[:2000]
            if final_attempt:
                p.status, p.platform_state = "failed", None if not p.external_id else p.platform_state
        if final_attempt:
            audit.record("post.failed", "post", post_id, code=code, error=str(e)[:500])
        raise
    except Exception as e:
        code = getattr(e, "code", "publish_error")
        with db.session() as s:
            p = s.get(Post, post_id)
            assert p is not None
            p.status, p.error, p.error_code = "failed", f"{type(e).__name__}: {e}"[:2000], code
            if getattr(e, "reconnect", False):
                a = s.get(PublishAccount, acc.id)
                if a is not None:
                    a.status = "reconnect_required"
        audit.record("post.failed", "post", post_id, code=code, error=str(e)[:500])
        raise
    with db.session() as s:
        p = s.get(Post, post_id)
        assert p is not None
        p.status = res.status if res.status in ("published", "scheduled") else "publishing"
        p.external_id, p.url, p.raw_response = res.external_id, res.url, res.raw
        p.platform_state = res.platform_state or ("public" if p.visibility == "public" else p.visibility)
        p.warnings = res.warnings
        p.error, p.error_code = None, None
        if res.status == "published":
            p.published_at = utcnow()
        c = s.get(Clip, p.clip_id)
        if c is not None and p.visibility == "public" and res.status in ("published", "scheduled"):
            c.status = "published"
    audit.record("post.published" if res.status == "published" else "post.submitted", "post", post_id,
                 provider=acc.provider, external_id=res.external_id, url=res.url, platform_state=res.platform_state,
                 warnings=[w.get("code") for w in res.warnings])
    return {"post_id": post_id, "status": res.status, "url": res.url, "external_id": res.external_id,
            "platform_state": res.platform_state, "warnings": res.warnings}


def _account_ctx(acc: PublishAccount) -> dict[str, Any]:
    return account_dict(acc) | {"credential_ref": acc.credential_ref, "meta": acc.meta or {}}


def refresh_statuses() -> list[dict[str, Any]]:
    """Resolve posts still processing on the platform side, and YouTube uploads whose platform state
    can still change (processing, or scheduled and due)."""
    out = []
    with db.session() as s:
        pending = list(s.scalars(select(Post).where(Post.external_id.is_not(None), or_(
            Post.status == "publishing",
            and_(Post.provider == "youtube", Post.platform_state == "processing"),
            and_(Post.provider == "youtube", Post.status == "scheduled", Post.scheduled_at <= utcnow())))))
    for post in pending:
        acc = _get_account(post.account_id) if post.account_id else None
        if acc is None:
            continue
        pub = _publisher(acc.provider)
        try:
            res = pub.refresh_status(_account_ctx(acc), post.external_id)
        except PublishError as e:
            with db.session() as s:
                p = s.get(Post, post.id)
                assert p is not None
                p.error, p.error_code = str(e)[:2000], getattr(e, "code", "publish_error")
                if post.status == "publishing":
                    p.status = "failed"
            out.append({"post_id": post.id, "status": "failed", "error_code": getattr(e, "code", None)})
            continue
        if res is None:
            continue
        with db.session() as s:
            p = s.get(Post, post.id)
            assert p is not None
            before = (p.status, p.platform_state)
            if res.platform_state:
                p.platform_state = res.platform_state
            if res.status == "deleted":
                p.status = "deleted"
            elif res.status in ("failed", "rejected"):
                p.status, p.error_code = "failed", res.status
            elif res.status == "published" and p.status in ("publishing", "scheduled"):
                if p.status == "scheduled" and res.platform_state != "public":
                    pass                                   # not flipped yet
                else:
                    p.status, p.url = "published", res.url or p.url
                    p.published_at = p.published_at or utcnow()
            changed = before != (p.status, p.platform_state)
        if changed:
            out.append({"post_id": post.id, "status": p.status, "platform_state": p.platform_state, "url": res.url})
    return out


def cancel_post(post_id: int, actor: str = "user") -> Post:
    """Cancel a scheduled post. A YouTube video already uploaded with publishAt has its schedule
    removed on YouTube (it stays private), which needs the manage permission."""
    post = get_post(post_id)
    if post.status not in ("scheduled", "failed"):
        raise ValueError(f"post {post_id} is {post.status}; only scheduled or failed posts can be cancelled")
    if post.provider == "youtube" and post.external_id and post.status == "scheduled":
        acc = _get_account(post.account_id)  # type: ignore[arg-type]
        _youtube().update_video(_account_ctx(acc), post.external_id,
                                           publish_at=None)
    with db.session() as s:
        p = s.get(Post, post_id)
        assert p is not None
        p.status = "cancelled"
        if p.external_id and p.provider == "youtube":
            p.platform_state = "private"
    audit.record("post.cancelled", "post", post_id, actor=actor)
    return get_post(post_id)


def reschedule_post(post_id: int, when: datetime | str, tz: str | None = None, actor: str = "user") -> Post:
    post = get_post(post_id)
    if post.status != "scheduled":
        raise ValueError(f"post {post_id} is {post.status}; only scheduled posts can be rescheduled")
    new = _parse_when(when, tz or post.timezone)
    if post.provider == "youtube" and post.external_id:
        acc = _get_account(post.account_id)  # type: ignore[arg-type]
        _youtube().update_video(_account_ctx(acc), post.external_id,
                                           publish_at=new)
    with db.session() as s:
        p = s.get(Post, post_id)
        assert p is not None
        p.scheduled_at = new
    audit.record("post.rescheduled", "post", post_id, actor=actor, scheduled_at=new.isoformat())
    return get_post(post_id)


def update_post(post_id: int, *, title: str | None = None, description: str | None = None,
                tags: list[str] | None = None, privacy: str | None = None, confirm: bool = False,
                actor: str = "user") -> dict[str, Any]:
    """Edit a video already on the platform (YouTube). A dry run unless confirm=True."""
    post = get_post(post_id)
    if post.provider != "youtube" or not post.external_id:
        raise ValueError(f"post {post_id} isn't an uploaded YouTube video")
    changes = {k: v for k, v in {"title": title, "description": description, "tags": tags,
                                  "privacy": privacy}.items() if v is not None}
    if not changes:
        raise ValueError("nothing to change")
    if not confirm:
        return {"post_id": post_id, "dry_run": True, "changes": changes, "external_id": post.external_id}
    acc = _get_account(post.account_id)  # type: ignore[arg-type]
    res = _youtube().update_video(_account_ctx(acc), post.external_id, title=title, description=description,
                                  tags=tags, privacy=privacy)
    with db.session() as s:
        p = s.get(Post, post_id)
        assert p is not None
        if title is not None:
            p.title = res.get("snippet", {}).get("title", title)
        if privacy is not None:
            p.visibility, p.platform_state = privacy, privacy
    audit.record("post.updated", "post", post_id, actor=actor, fields=sorted(changes))
    return {"post_id": post_id, "updated": sorted(changes), "external_id": post.external_id}


def delete_post(post_id: int, confirm: bool = False, actor: str = "user") -> dict[str, Any]:
    """Delete the video on the platform (YouTube). Irreversible, so a dry run unless confirm=True."""
    post = get_post(post_id)
    if post.provider != "youtube" or not post.external_id:
        raise ValueError(f"post {post_id} isn't an uploaded YouTube video")
    if post.status == "deleted":
        return {"post_id": post_id, "status": "deleted", "note": "already deleted"}
    if not confirm:
        return {"post_id": post_id, "dry_run": True, "would_delete": post.url or post.external_id,
                "note": "permanent: YouTube can't restore a deleted video"}
    acc = _get_account(post.account_id)  # type: ignore[arg-type]
    _youtube().delete_video(_account_ctx(acc), post.external_id)
    with db.session() as s:
        p = s.get(Post, post_id)
        assert p is not None
        p.status, p.platform_state = "deleted", "deleted"
    audit.record("post.deleted", "post", post_id, actor=actor, external_id=post.external_id)
    return {"post_id": post_id, "status": "deleted"}


def account_health(account_id: int, check_live: bool = False) -> dict[str, Any]:
    """What the dashboard, CLI and MCP show for a connected account. Never returns token values."""
    acc = _get_account(account_id)
    tok = secrets.get(acc.credential_ref) if acc.credential_ref else None
    tok = tok if isinstance(tok, dict) else {}
    scopes = (acc.meta or {}).get("scopes") or tok.get("scopes") or []
    from .youtube import ANALYTICS, MANAGE, UPLOAD

    out: dict[str, Any] = {
        "account_id": acc.id, "platform": acc.platform, "provider": acc.provider, "handle": acc.handle,
        "status": acc.status, "channel_id": (acc.meta or {}).get("channel_id"),
        "has_token": bool(tok), "has_refresh_token": bool(tok.get("refresh_token")),
        "access_expires_at": datetime.fromtimestamp(tok["expires_at"], tz=UTC).isoformat()
        if tok.get("expires_at") else None,
        "scopes": scopes,
    }
    if acc.provider == "youtube":
        out.update(can_upload=UPLOAD in scopes, can_read_analytics=ANALYTICS in scopes,
                   can_manage=MANAGE in scopes,
                   mode="public allowed (audited)" if get_settings().youtube_public_allowed else "private only")
    if check_live and acc.provider == "youtube":
        try:
            ch = _youtube().channel(_account_ctx(acc))
            out["live_check"] = {"ok": True, "channel_title": (ch or {}).get("snippet", {}).get("title")}
        except (PublishError, jobs.RetryableError) as e:
            out["live_check"] = {"ok": False, "error_code": getattr(e, "code", None), "error": str(e)[:300]}
            if getattr(e, "reconnect", False):
                with db.session() as s:
                    a = s.get(PublishAccount, acc.id)
                    if a is not None:
                        a.status = "reconnect_required"
                out["status"] = "reconnect_required"
    return out


def test_private_upload(account_id: int, confirm: bool = False, delete_after: bool = False,
                        actor: str = "user") -> dict[str, Any]:
    """Upload a 5-second generated test pattern as a PRIVATE video, to prove the connection works
    end to end. Never uses real footage. A dry run unless confirm=True; `delete_after` removes it
    again (needs the manage permission)."""
    import subprocess
    import tempfile
    from pathlib import Path

    acc = _get_account(account_id)
    if acc.provider != "youtube":
        raise ValueError("the test upload is for YouTube accounts")
    title = "Ezra connection test (private)"
    plan = {"account": acc.handle, "visibility": "private", "title": title,
            "video": "5 s generated test pattern with a tone, 1080x1920", "delete_after": delete_after}
    if not confirm:
        return {"dry_run": True, **plan}
    with tempfile.TemporaryDirectory() as tmp:
        video, thumb = Path(tmp) / "ezra-test.mp4", Path(tmp) / "ezra-test.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "5", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-frames:v", "1", "-vf", "scale=720:1280",
                        str(thumb)], check=True)
        req = PostRequest(video=video, title=title, caption="Ezra connection test",
                          description="Uploaded by Ezra to check the YouTube connection. Private.",
                          hashtags=["ezra"], visibility="private", thumbnail=thumb)
        audit.record("youtube.test_upload_started", "publish_account", account_id, actor=actor)
        res = _youtube().publish(_account_ctx(acc), req, "youtube")
    out = {"video_id": res.external_id, "url": res.url, "platform_state": res.platform_state,
           "warnings": res.warnings, "deleted": False}
    video_info = _youtube().video(_account_ctx(acc), res.external_id or "")
    out["verified_on_youtube"] = bool(video_info)
    out["privacy"] = ((video_info or {}).get("status") or {}).get("privacyStatus")
    if delete_after and res.external_id:
        _youtube().delete_video(_account_ctx(acc), res.external_id)
        out["deleted"] = True
    audit.record("youtube.test_upload", "publish_account", account_id, actor=actor,
                 video_id=res.external_id, deleted=out["deleted"])
    return out


def disconnect_account(account_id: int, revoke: bool = True, purge_data: bool = False,
                       actor: str = "user") -> dict[str, Any]:
    """Revoke the platform token (when supported), delete it from the secret store and mark the
    account disconnected. With purge_data, also delete what Ezra stored from the platform's API for
    this account: metric snapshots and raw API responses (post records keep only Ezra's own data)."""
    acc = _get_account(account_id)
    purged = 0
    if purge_data:
        from ..db.models import MetricSnapshot

        with db.session() as s:
            post_ids = list(s.scalars(select(Post.id).where(Post.account_id == account_id)))
            if post_ids:
                purged = s.query(MetricSnapshot).filter(MetricSnapshot.post_id.in_(post_ids)).delete(
                    synchronize_session=False)
                for p in s.scalars(select(Post).where(Post.id.in_(post_ids))):
                    p.raw_response, p.upload_state = {}, {}
    revoked = False
    if acc.credential_ref:
        tok = secrets.get(acc.credential_ref)
        if revoke and isinstance(tok, dict) and hasattr(_publisher(acc.provider), "revoke"):
            try:
                revoked = bool(_publisher(acc.provider).revoke(tok))
            except httpx.HTTPError:
                revoked = False
        secrets.delete(acc.credential_ref)
    with db.session() as s:
        a = s.get(PublishAccount, account_id)
        assert a is not None
        a.status, a.credential_ref = "disconnected", None
        cred = s.scalar(select(IntegrationCredentialMetadata).where(
            IntegrationCredentialMetadata.provider == acc.provider,
            IntegrationCredentialMetadata.account_label == acc.handle))
        if cred is not None:
            cred.status = "revoked"
    audit.record("account.disconnected", "publish_account", account_id, actor=actor, revoked=revoked,
                 purged_snapshots=purged)
    return {"account_id": account_id, "status": "disconnected", "token_revoked": revoked,
            "purged_snapshots": purged if purge_data else None}


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
            "actual_payout": p.actual_payout, "created_at": iso(p.created_at),
            "error_code": p.error_code, "platform_state": p.platform_state, "warnings": p.warnings or [],
            "upload_progress": _progress(p.upload_state)}


def _progress(state: dict[str, Any] | None) -> float | None:
    """Fraction uploaded. The session URI itself stays out of API responses (it grants upload access)."""
    if not state or not state.get("size"):
        return None
    return 1.0 if state.get("video_id") else round(min(1.0, state.get("bytes_sent", 0) / state["size"]), 3)
