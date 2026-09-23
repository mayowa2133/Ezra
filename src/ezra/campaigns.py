"""Campaigns: first-class entities with normalized fields, the raw instructions
they came from, and a list of enforceable CampaignRules derived from them.

Import formats: YAML, JSON, CSV (one campaign per row) and the manual form in
the API/dashboard. Marketplaces plug in through CampaignSourceAdapter; Ezra
ships only the file adapter. It does not scrape marketplace websites: campaigns
without a public API are entered or imported by hand.
"""

from __future__ import annotations

import csv
import io
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from . import db
from .db.models import Campaign, CampaignRule

DEFAULT_WEIGHTS: dict[str, float] = {
    "hook": 0.22, "retention": 0.18, "context": 0.14, "emotion": 0.09, "novelty": 0.08,
    "discussion": 0.08, "payoff": 0.08, "visual": 0.05, "campaign_fit": 0.08,
}

PLATFORMS = {"tiktok", "instagram", "youtube", "x", "linkedin", "facebook", "threads"}


class CampaignSpec(BaseModel):
    """Import schema. Accepts the nested YAML shape (rate.cpm, requirements.*)
    and flat fields alike; see campaigns/example.yaml."""
    id: str | None = Field(None, description="Slug; derived from name when omitted")
    name: str
    description: str | None = None
    provider: str | None = Field(None, description="Marketplace/programme, e.g. 'Content Rewards'")
    source_authorization: str = "campaign_supplied"
    payout_type: str = "cpm"
    cpm: float = 0.0
    minimum_views: int = 0
    maximum_payout: float | None = None
    budget: float | None = None
    currency: str = "USD"
    tracking_window_days: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    platforms: list[str] = Field(default_factory=lambda: ["tiktok", "instagram", "youtube"])
    min_duration: float = 15
    max_duration: float = 60
    hashtags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    cta: str | None = None
    subtitles_required: bool = True
    logo_required: bool = False
    allowed_speakers: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list, description="Free-text prohibitions, e.g. 'profanity'")
    forbidden_words: list[str] = Field(default_factory=list)
    forbidden_topics: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    geography: list[str] = Field(default_factory=list)
    posting_limits: dict[str, Any] = Field(default_factory=dict)
    human_approval_required: bool = True
    rules: list[str] = Field(default_factory=list, description="Additional free-form rules")
    brief: str | None = None
    weights: dict[str, float] = Field(default_factory=dict)
    brand_kit: str | None = None
    source: list[str] = Field(default_factory=list, description="Source files to ingest on import")

    @field_validator("hashtags")
    @classmethod
    def _hashtags(cls, v: list[str]) -> list[str]:
        return [t if t.startswith("#") else f"#{t}" for t in (x.strip() for x in v) if t]

    @field_validator("mentions")
    @classmethod
    def _mentions(cls, v: list[str]) -> list[str]:
        return [m if m.startswith("@") else f"@{m}" for m in (x.strip() for x in v) if m]

    @field_validator("platforms")
    @classmethod
    def _platforms(cls, v: list[str]) -> list[str]:
        out = [p.strip().lower().replace("youtube shorts", "youtube").replace("instagram reels", "instagram")
               for p in v]
        bad = [p for p in out if p not in PLATFORMS]
        if bad:
            raise ValueError(f"unknown platform(s) {bad}; known: {sorted(PLATFORMS)}")
        return out

    @field_validator("weights")
    @classmethod
    def _weights(cls, w: dict[str, float]) -> dict[str, float]:
        unknown = set(w) - set(DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"unknown weight keys {sorted(unknown)}; allowed {sorted(DEFAULT_WEIGHTS)}")
        return w

    @property
    def slug(self) -> str:
        return self.id or slugify(self.name)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "campaign"


def normalized_weights(overrides: dict[str, float] | None) -> dict[str, float]:
    merged = {**DEFAULT_WEIGHTS, **(overrides or {})}
    total = sum(merged.values())
    return {k: v / total for k, v in merged.items()}


# --- parsing -------------------------------------------------------------------

_NESTED = {("rate", "cpm"): "cpm", ("requirements", "min_duration"): "min_duration",
           ("requirements", "max_duration"): "max_duration", ("payout", "cpm"): "cpm",
           ("payout", "minimum_views"): "minimum_views", ("payout", "maximum"): "maximum_payout"}
_ALIASES = {"minimum_qualified_views": "minimum_views", "min_views": "minimum_views",
            "max_payout": "maximum_payout", "maximum_payout_per_clip": "maximum_payout",
            "required_hashtags": "hashtags", "required_mentions": "mentions", "required_cta": "cta",
            "allowed_platforms": "platforms", "slug": "id", "sources": "source"}


def _flatten(data: dict[str, Any]) -> dict[str, Any]:
    data = dict(data)
    if isinstance(data.get("campaign"), dict):
        data = dict(data["campaign"])
    for (outer, inner), flat in _NESTED.items():
        if isinstance(data.get(outer), dict) and inner in data[outer]:
            data.setdefault(flat, data[outer][inner])
    for outer in ("rate", "requirements", "payout"):
        if isinstance(data.get(outer), dict):
            data.pop(outer)
    for alias, flat in _ALIASES.items():
        if alias in data and flat not in data:
            data[flat] = data.pop(alias)
    # `required:` free-text list from the original clipper format
    for item in data.pop("required", []) or []:
        low = str(item).lower()
        if "subtitle" in low or "caption" in low:
            data["subtitles_required"] = True
        elif "logo" in low:
            data["logo_required"] = True
        elif "hashtag" in low:
            pass  # enforced through `hashtags`
        else:
            data.setdefault("rules", []).append(str(item))
    return data


def parse(text: str, fmt: str | None = None) -> list[CampaignSpec]:
    """Parse YAML / JSON / CSV campaign definitions."""
    fmt = (fmt or _sniff(text)).lower()
    if fmt == "csv":
        return [parse_csv_row(row) for row in csv.DictReader(io.StringIO(text))]
    data = json.loads(text) if fmt == "json" else yaml.safe_load(text)
    items = data if isinstance(data, list) else (data.get("campaigns") if isinstance(data, dict) and
                                                 isinstance(data.get("campaigns"), list) else [data])
    return [CampaignSpec.model_validate(_flatten(item)) for item in items]


def _sniff(text: str) -> str:
    t = text.lstrip()
    if t.startswith(("{", "[")):
        return "json"
    first = t.splitlines()[0] if t else ""
    if "," in first and ":" not in first:
        return "csv"
    return "yaml"


_LIST_COLUMNS = {"platforms", "hashtags", "mentions", "allowed_speakers", "forbidden", "forbidden_words",
                 "forbidden_topics", "competitors", "geography", "rules", "source"}


def parse_csv_row(row: dict[str, str]) -> CampaignSpec:
    data: dict[str, Any] = {}
    for k, v in row.items():
        if k is None or v is None or v.strip() == "":
            continue
        k = _ALIASES.get(k.strip(), k.strip())
        data[k] = [x.strip() for x in re.split(r"[;|]", v) if x.strip()] if k in _LIST_COLUMNS else v.strip()
    return CampaignSpec.model_validate(data)


def load_file(path: Path) -> list[CampaignSpec]:
    fmt = {".yaml": "yaml", ".yml": "yaml", ".json": "json", ".csv": "csv"}.get(path.suffix.lower())
    return parse(path.read_text(), fmt)


# --- rules ---------------------------------------------------------------------

def derive_rules(spec: CampaignSpec) -> list[dict[str, Any]]:
    """Normalized, enforceable rules. severity: fail blocks, review needs a human,
    info is surfaced but never blocks."""
    rules: list[dict[str, Any]] = [
        dict(kind="duration", params={"min": spec.min_duration, "max": spec.max_duration},
             description=f"Clip must be {spec.min_duration:g}-{spec.max_duration:g}s"),
        dict(kind="source_rights", params={"basis": spec.source_authorization},
             description="Source footage must be authorized for this campaign"),
        dict(kind="platforms", params={"allowed": spec.platforms},
             description=f"Post only to {', '.join(spec.platforms)}"),
    ]
    free = " ".join(spec.forbidden).lower()
    if any(w in free for w in ("profan", "swear", "curs", "explicit language")):
        rules.append(dict(kind="profanity", params={}, description="No profanity"))
    words = list(spec.forbidden_words)
    for item in spec.forbidden:
        low = item.lower()
        if any(w in low for w in ("profan", "swear", "curs", "competitor")):
            continue
        rules.append(dict(kind="forbidden_topic", params={"topic": item}, severity="review",
                          description=f"Forbidden: {item}"))
    if words:
        rules.append(dict(kind="forbidden_words", params={"words": words}, description="Forbidden words"))
    if spec.competitors or "competitor" in free:
        rules.append(dict(kind="competitors", params={"names": spec.competitors},
                          description="No competitor mentions",
                          severity="fail" if spec.competitors else "review"))
    for topic in spec.forbidden_topics:
        rules.append(dict(kind="forbidden_topic", params={"topic": topic}, severity="review",
                          description=f"Forbidden topic: {topic}"))
    if spec.hashtags:
        rules.append(dict(kind="hashtags", params={"tags": spec.hashtags},
                          description=f"Caption must include {' '.join(spec.hashtags)}"))
    if spec.mentions:
        rules.append(dict(kind="mentions", params={"handles": spec.mentions},
                          description=f"Caption must mention {' '.join(spec.mentions)}"))
    if spec.cta:
        rules.append(dict(kind="cta", params={"text": spec.cta}, severity="review",
                          description=f"Include the CTA: {spec.cta}"))
    if spec.subtitles_required:
        rules.append(dict(kind="subtitles", params={}, description="Burned-in subtitles required"))
    if spec.logo_required:
        rules.append(dict(kind="logo", params={}, description="Brand logo required on the video"))
    if spec.allowed_speakers:
        rules.append(dict(kind="speakers", params={"allowed": spec.allowed_speakers}, severity="review",
                          description=f"Only these speakers: {', '.join(spec.allowed_speakers)}"))
    if spec.starts_at or spec.ends_at:
        rules.append(dict(kind="window", params={"starts": spec.starts_at.isoformat() if spec.starts_at else None,
                                                  "ends": spec.ends_at.isoformat() if spec.ends_at else None},
                          description="Post only inside the campaign window"))
    if spec.posting_limits:
        rules.append(dict(kind="posting_limits", params=spec.posting_limits,
                          description=f"Posting limits: {spec.posting_limits}"))
    if spec.geography:
        rules.append(dict(kind="geography", params={"regions": spec.geography}, severity="info",
                          description=f"Geography: {', '.join(spec.geography)} (not verifiable locally)"))
    for text in spec.rules:
        rules.append(dict(kind="freeform", params={"text": text}, severity="review", description=text))
    for r in rules:
        r.setdefault("severity", "fail")
    return rules


# --- persistence ---------------------------------------------------------------

def upsert(spec: CampaignSpec, raw: str | None = None) -> Campaign:
    """Create or update by slug; rules are re-derived (manual rules are kept)."""
    from .brandkits import get_brand_kit

    with db.session() as s:
        c = s.scalar(select(Campaign).where(Campaign.slug == spec.slug))
        if c is None:
            c = Campaign(slug=spec.slug, name=spec.name)
            s.add(c)
        c.name = spec.name
        c.description = spec.description
        c.provider = spec.provider
        c.source_authorization = spec.source_authorization
        c.payout_type = spec.payout_type
        c.cpm = spec.cpm
        c.min_qualified_views = spec.minimum_views
        c.max_payout_per_clip = spec.maximum_payout
        c.budget = spec.budget
        c.currency = spec.currency
        c.tracking_window_days = spec.tracking_window_days
        c.starts_at = spec.starts_at
        c.ends_at = spec.ends_at
        c.allowed_platforms = spec.platforms
        c.min_duration = spec.min_duration
        c.max_duration = spec.max_duration
        c.required_hashtags = spec.hashtags
        c.required_mentions = spec.mentions
        c.required_cta = spec.cta
        c.subtitles_required = spec.subtitles_required
        c.logo_required = spec.logo_required
        c.allowed_speakers = spec.allowed_speakers
        c.forbidden_words = spec.forbidden_words
        c.forbidden_topics = spec.forbidden_topics + [
            f for f in spec.forbidden if not any(w in f.lower() for w in ("profan", "swear", "curs", "competitor"))]
        c.competitors = spec.competitors
        c.geography = spec.geography
        c.posting_limits = spec.posting_limits
        c.human_approval_required = spec.human_approval_required
        c.extra_rules = spec.rules
        c.brief = spec.brief
        c.weights = spec.weights
        c.raw_instructions = raw if raw is not None else spec.model_dump_json(indent=2)
        if spec.brand_kit:
            c.brand_kit_id = get_brand_kit(spec.brand_kit).id
        s.flush()
        for rule in [r for r in c.rules if r.origin != "manual"]:
            s.delete(rule)
        s.flush()
        for r in derive_rules(spec):
            s.add(CampaignRule(campaign_id=c.id, origin="normalized", **r))
    return get(spec.slug)


def import_text(text: str, fmt: str | None = None, base_dir: Path | None = None,
                ingest_sources: bool = True) -> list[Campaign]:
    out = []
    for spec in parse(text, fmt):
        camp = upsert(spec, raw=text)
        out.append(camp)
        if ingest_sources and spec.source:
            from . import sources

            for src in spec.source:
                path = Path(src).expanduser()
                if not path.is_absolute() and base_dir is not None:
                    path = base_dir / path
                if path.exists():
                    sources.ingest(path, campaign=camp.slug, rights_basis=spec.source_authorization)
    return out


def import_file(path: Path, ingest_sources: bool = True) -> list[Campaign]:
    fmt = {".yaml": "yaml", ".yml": "yaml", ".json": "json", ".csv": "csv"}.get(path.suffix.lower())
    return import_text(path.read_text(), fmt, base_dir=path.parent.resolve(), ingest_sources=ingest_sources)


def get(ref: str | int) -> Campaign:
    with db.session() as s:
        q = select(Campaign).where((Campaign.slug == str(ref)) | (Campaign.id == _int(ref)))
        c = s.scalar(q)
        if c is None:
            raise LookupError(f"no campaign {ref!r} (see `ezra campaign list`)")
        _ = c.rules, c.brand_kit  # load relationships before the session closes
        return c


def _int(ref: str | int) -> int:
    try:
        return int(ref)
    except (TypeError, ValueError):
        return -1


def list_campaigns() -> list[Campaign]:
    with db.session() as s:
        items = list(s.scalars(select(Campaign).order_by(Campaign.id)))
        for c in items:
            _ = c.rules
        return items


def add_rule(ref: str | int, kind: str, params: dict[str, Any], severity: str = "fail",
             description: str = "") -> CampaignRule:
    if severity not in ("fail", "review", "info"):
        raise ValueError("severity must be fail | review | info")
    c = get(ref)
    with db.session() as s:
        r = CampaignRule(campaign_id=c.id, kind=kind, params=params, severity=severity,
                         origin="manual", description=description or kind)
        s.add(r)
        s.flush()
        return r


def to_dict(c: Campaign) -> dict[str, Any]:
    return {
        "id": c.id, "slug": c.slug, "name": c.name, "description": c.description, "provider": c.provider,
        "status": c.status, "source_authorization": c.source_authorization, "payout_type": c.payout_type,
        "cpm": c.cpm, "min_qualified_views": c.min_qualified_views, "max_payout_per_clip": c.max_payout_per_clip,
        "budget": c.budget, "currency": c.currency, "tracking_window_days": c.tracking_window_days,
        "starts_at": c.starts_at.isoformat() if c.starts_at else None,
        "ends_at": c.ends_at.isoformat() if c.ends_at else None,
        "platforms": c.allowed_platforms, "min_duration": c.min_duration, "max_duration": c.max_duration,
        "hashtags": c.required_hashtags, "mentions": c.required_mentions, "cta": c.required_cta,
        "subtitles_required": c.subtitles_required, "logo_required": c.logo_required,
        "allowed_speakers": c.allowed_speakers, "forbidden_words": c.forbidden_words,
        "forbidden_topics": c.forbidden_topics, "competitors": c.competitors, "geography": c.geography,
        "posting_limits": c.posting_limits, "human_approval_required": c.human_approval_required,
        "extra_rules": c.extra_rules, "brief": c.brief, "weights": normalized_weights(c.weights),
        "brand_kit_id": c.brand_kit_id,
        "rules": [{"id": r.id, "kind": r.kind, "params": r.params, "severity": r.severity,
                   "origin": r.origin, "description": r.description} for r in c.rules],
    }


# --- marketplace adapters ------------------------------------------------------

class CampaignSourceAdapter(ABC):
    """A place campaigns come from. Implementations must use a public API or
    files the user supplies; scraping marketplace websites is out of scope."""
    name: str

    @abstractmethod
    def fetch(self) -> list[CampaignSpec]: ...


class FileCampaignAdapter(CampaignSourceAdapter):
    """Every *.yaml / *.yml / *.json / *.csv in a directory."""
    name = "files"

    def __init__(self, directory: Path):
        self.directory = directory

    def fetch(self) -> list[CampaignSpec]:
        out: list[CampaignSpec] = []
        for p in sorted(self.directory.iterdir()):
            if p.suffix.lower() in (".yaml", ".yml", ".json", ".csv"):
                out.extend(load_file(p))
        return out


def sync_adapter(adapter: CampaignSourceAdapter) -> list[Campaign]:
    return [upsert(spec) for spec in adapter.fetch()]
