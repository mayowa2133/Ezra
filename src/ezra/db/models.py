"""Ezra's persistent state (SQLAlchemy 2.0). Works on PostgreSQL (Docker) and
SQLite (local dev, tests); Alembic migrations in ezra/db/migrations are the
source of truth for the schema.

Scores, compliance results and metrics are stored factor by factor with their
provenance, never as a single opaque number."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# --- campaigns ---------------------------------------------------------------

class BrandKit(Timestamped, Base):
    __tablename__ = "brand_kits"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    logo_key: Mapped[str | None] = mapped_column(String(500))
    logo_position: Mapped[str] = mapped_column(String(20), default="top-right")
    fonts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)        # {"caption": path, "title": path}
    colors: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)       # {"primary": "#hex", "accent": ...}
    caption_theme: Mapped[str] = mapped_column(String(50), default="bold")
    intro_key: Mapped[str | None] = mapped_column(String(500))
    outro_key: Mapped[str | None] = mapped_column(String(500))
    default_layout: Mapped[str] = mapped_column(String(20), default="auto")
    cta_text: Mapped[str | None] = mapped_column(Text)
    watermark_text: Mapped[str | None] = mapped_column(String(200))
    safe_zone: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)    # {"top": 0.08, "bottom": 0.18, ...}


class Campaign(Timestamped, Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(120))                 # e.g. "Content Rewards"
    status: Mapped[str] = mapped_column(String(20), default="active")
    source_authorization: Mapped[str] = mapped_column(String(30), default="campaign_supplied")
    payout_type: Mapped[str] = mapped_column(String(30), default="cpm")
    cpm: Mapped[float] = mapped_column(Float, default=0.0)
    min_qualified_views: Mapped[int] = mapped_column(Integer, default=0)
    max_payout_per_clip: Mapped[float | None] = mapped_column(Float)
    budget: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    tracking_window_days: Mapped[int | None] = mapped_column(Integer)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    allowed_platforms: Mapped[list[Any]] = mapped_column(JSON, default=list)
    min_duration: Mapped[float] = mapped_column(Float, default=15)
    max_duration: Mapped[float] = mapped_column(Float, default=60)
    required_hashtags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    required_mentions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    required_cta: Mapped[str | None] = mapped_column(Text)
    subtitles_required: Mapped[bool] = mapped_column(Boolean, default=True)
    logo_required: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_speakers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    forbidden_words: Mapped[list[Any]] = mapped_column(JSON, default=list)
    forbidden_topics: Mapped[list[Any]] = mapped_column(JSON, default=list)
    competitors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    geography: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # {"per_day": 3, "per_platform_per_day": {...}}
    posting_limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    human_approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    extra_rules: Mapped[list[Any]] = mapped_column(JSON, default=list)          # free-form rule strings
    raw_instructions: Mapped[str | None] = mapped_column(Text)                  # as supplied (YAML/JSON/text)
    brief: Mapped[str | None] = mapped_column(Text)
    weights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    brand_kit_id: Mapped[int | None] = mapped_column(ForeignKey("brand_kits.id"))

    rules: Mapped[list[CampaignRule]] = relationship(back_populates="campaign", cascade="all, delete-orphan")
    brand_kit: Mapped[BrandKit | None] = relationship()


class CampaignRule(Base):
    """One enforceable rule. `severity` fail = hard block, review = needs a human."""
    __tablename__ = "campaign_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(50))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    severity: Mapped[str] = mapped_column(String(10), default="fail")
    origin: Mapped[str] = mapped_column(String(20), default="normalized")   # normalized | extracted | manual
    description: Mapped[str] = mapped_column(Text, default="")
    campaign: Mapped[Campaign] = relationship(back_populates="rules")


# --- sources & analysis ------------------------------------------------------

class Source(Timestamped, Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("campaign_id", "sha256"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    storage_key: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str | None] = mapped_column(String(500))
    origin_url: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[float] = mapped_column(Float, default=0)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=True)
    rights_status: Mapped[str] = mapped_column(String(20), default="unverified")  # authorized | unverified | rejected
    rights_basis: Mapped[str | None] = mapped_column(String(40))  # campaign_supplied | owned | licensed | permission
    rights_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="ingested")  # ingested | analyzing | analyzed | failed


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (UniqueConstraint("source_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(64))          # hash of provider+model+settings: cache key
    language: Mapped[str | None] = mapped_column(String(10))
    duration: Mapped[float] = mapped_column(Float, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    has_speakers: Mapped[bool] = mapped_column(Boolean, default=False)
    diarizer: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    segments: Mapped[list[TranscriptSegment]] = relationship(
        back_populates="transcript", cascade="all, delete-orphan", order_by="TranscriptSegment.idx")


class TranscriptSegment(Base):
    """A sentence-level segment. words: [{w, s, e, p (probability), spk}]."""
    __tablename__ = "transcript_segments"
    id: Mapped[int] = mapped_column(primary_key=True)
    transcript_id: Mapped[int] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    speaker: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None] = mapped_column(Float)
    sentence_end: Mapped[bool] = mapped_column(Boolean, default=True)
    words: Mapped[list[Any]] = mapped_column(JSON, default=list)
    transcript: Mapped[Transcript] = relationship(back_populates="segments")


class SourceAnalysis(Base):
    """One analysis product (scenes, faces, silence, speakers, topics, audio, visual)
    with provider, version, confidence and the data itself."""
    __tablename__ = "source_analyses"
    __table_args__ = (UniqueConstraint("source_id", "kind", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(60))
    version: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- candidates & clips ------------------------------------------------------

class Candidate(Timestamped, Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    transcript: Mapped[str] = mapped_column(Text, default="")
    speakers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    topic: Mapped[str | None] = mapped_column(String(300))
    hook: Mapped[str | None] = mapped_column(Text)
    hook_type: Mapped[str | None] = mapped_column(String(30))
    title: Mapped[str | None] = mapped_column(String(300))
    reason: Mapped[str | None] = mapped_column(Text)
    context_before: Mapped[str | None] = mapped_column(Text)
    context_after: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(30), default="scout")       # scout | agent | openshorts | manual | live
    origin_ref: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="new")         # new | ranked | rendered | discarded
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)   # cheap deterministic features
    # score factors (0-100), each persisted
    content_score: Mapped[float | None] = mapped_column(Float)
    hook_score: Mapped[float | None] = mapped_column(Float)
    retention_score: Mapped[float | None] = mapped_column(Float)
    emotion_score: Mapped[float | None] = mapped_column(Float)
    context_score: Mapped[float | None] = mapped_column(Float)
    discussion_score: Mapped[float | None] = mapped_column(Float)
    novelty_score: Mapped[float | None] = mapped_column(Float)
    payoff_score: Mapped[float | None] = mapped_column(Float)
    visual_score: Mapped[float | None] = mapped_column(Float)
    campaign_fit_score: Mapped[float | None] = mapped_column(Float)
    performance_prior: Mapped[float | None] = mapped_column(Float)
    diversity_penalty: Mapped[float | None] = mapped_column(Float)
    rank_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    scorer: Mapped[str | None] = mapped_column(String(60))
    score_explanations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    compliance_status: Mapped[str | None] = mapped_column(String(20))    # PASS | FAIL | REVIEW_REQUIRED
    compliance_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    expected_value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Clip(Timestamped, Base):
    __tablename__ = "clips"
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="rendering")
    # rendering | rendered | failed | approved | rejected | published | exported
    current_version_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    platform_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"youtube": {"title":..}, ...}
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(120))
    versions: Mapped[list[ClipVersion]] = relationship(
        back_populates="clip", cascade="all, delete-orphan", order_by="ClipVersion.version")
    candidate: Mapped[Candidate] = relationship()


class ClipVersion(Base):
    """A render of a clip with the exact spec that produced it (layout, captions,
    edits, brand kit, experiment variant), so results can be traced to choices."""
    __tablename__ = "clip_versions"
    __table_args__ = (UniqueConstraint("clip_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")   # pending | rendered | failed
    video_key: Mapped[str | None] = mapped_column(String(500))
    thumbnail_key: Mapped[str | None] = mapped_column(String(500))
    srt_key: Mapped[str | None] = mapped_column(String(500))
    ass_key: Mapped[str | None] = mapped_column(String(500))
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    layout_used: Mapped[str | None] = mapped_column(String(30))
    edit_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)   # removed silence/fillers, broll...
    render_seconds: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    clip: Mapped[Clip] = relationship(back_populates="versions")


# --- publishing & performance ------------------------------------------------

class PublishAccount(Timestamped, Base):
    __tablename__ = "publish_accounts"
    __table_args__ = (UniqueConstraint("platform", "handle"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(30))           # youtube | tiktok | instagram | ...
    provider: Mapped[str] = mapped_column(String(30))           # youtube | tiktok | instagram | upload-post | local
    handle: Mapped[str] = mapped_column(String(200))
    credential_ref: Mapped[str | None] = mapped_column(String(200))  # key in the secret store, never the secret
    status: Mapped[str] = mapped_column(String(20), default="connected")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Post(Timestamped, Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id"), index=True)
    clip_version_id: Mapped[int | None] = mapped_column(ForeignKey("clip_versions.id"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("publish_accounts.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str | None] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    # scheduled | publishing | published | failed | cancelled
    visibility: Mapped[str] = mapped_column(String(20), default="public")
    caption: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(300))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    experiment_id: Mapped[int | None] = mapped_column(ForeignKey("experiments.id"))
    experiment_variant: Mapped[str | None] = mapped_column(String(60))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)   # snapshot of what produced this post
    actual_payout: Mapped[float | None] = mapped_column(Float)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(40))            # provenance: youtube-api | manual | csv | ...
    views: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int | None] = mapped_column(Integer)
    avg_watch_seconds: Mapped[float | None] = mapped_column(Float)
    completion_rate: Mapped[float | None] = mapped_column(Float)
    followers_gained: Mapped[int | None] = mapped_column(Integer)
    retention: Mapped[list[Any] | None] = mapped_column(JSON)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Experiment(Timestamped, Base):
    __tablename__ = "experiments"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    factor: Mapped[str] = mapped_column(String(40))   # caption_theme | duration | hook | posting_time | layout | cta
    hypothesis: Mapped[str | None] = mapped_column(Text)
    variants: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{"key": "A", "value": ...}]
    status: Mapped[str] = mapped_column(String(20), default="running")
    min_samples_per_variant: Mapped[int] = mapped_column(Integer, default=5)


class RevenueRecord(Base):
    __tablename__ = "revenue_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"))
    kind: Mapped[str] = mapped_column(String(20), default="confirmed")   # confirmed | adjustment
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    source: Mapped[str] = mapped_column(String(40), default="manual")    # manual | csv | provider
    notes: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CostRecord(Base):
    __tablename__ = "cost_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))          # llm | transcription | render | storage | api | generative
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str] = mapped_column(String(30))          # tokens | seconds | bytes | calls
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), index=True)
    clip_id: Mapped[int | None] = mapped_column(ForeignKey("clips.id"), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Learning(Base):
    __tablename__ = "learnings"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(30), default="analyst")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- operations --------------------------------------------------------------

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # queued | running | completed | failed | cancelled
    priority: Mapped[int] = mapped_column(Integer, default=100)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)       # {"type", "message", "traceback", "step"}
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    worker: Mapped[str | None] = mapped_column(String(120))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobLog(Base):
    __tablename__ = "job_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(10), default="info")
    message: Mapped[str] = mapped_column(Text)


class IntegrationCredentialMetadata(Timestamped, Base):
    """What is connected, never the secret itself (that lives in the secret store
    under `secret_ref`)."""
    __tablename__ = "integration_credentials"
    __table_args__ = (UniqueConstraint("provider", "account_label"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    account_label: Mapped[str] = mapped_column(String(200))
    secret_ref: Mapped[str] = mapped_column(String(200))
    scopes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active")


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    code_verifier: Mapped[str | None] = mapped_column(String(200))
    account_label: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(120), default="system")
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(60))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
