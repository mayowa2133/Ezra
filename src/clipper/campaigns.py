"""Campaigns are first-class: their rules reach the judge before any clip is
proposed, and the deterministic checks in ranking.compliance enforce the
parts that can be enforced without judgment."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from . import db

DEFAULT_WEIGHTS: dict[str, float] = {
    "hook": 0.25,
    "retention": 0.20,
    "context": 0.15,
    "emotion": 0.10,
    "novelty": 0.10,
    "comment": 0.10,
    "campaign_fit": 0.10,
}


class Rate(BaseModel):
    cpm: float = 0.0


class Requirements(BaseModel):
    min_duration: float = 15
    max_duration: float = 60


class CampaignSpec(BaseModel):
    id: str | None = None
    name: str
    platform: str | None = None
    rate: Rate = Field(default_factory=Rate)
    minimum_views: int = 0
    maximum_payout: float | None = None
    budget: float | None = None
    requirements: Requirements = Field(default_factory=Requirements)
    forbidden: list[str] = Field(default_factory=list)
    forbidden_terms: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    required: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=lambda: ["tiktok", "instagram", "youtube"])
    source: list[str] = Field(default_factory=list)
    brief: str | None = None
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @field_validator("hashtags")
    @classmethod
    def _hash(cls, tags: list[str]) -> list[str]:
        return [t if t.startswith("#") else f"#{t}" for t in tags]

    @field_validator("weights")
    @classmethod
    def _weights(cls, w: dict[str, float]) -> dict[str, float]:
        unknown = set(w) - set(DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"unknown weight keys: {sorted(unknown)}; allowed: {sorted(DEFAULT_WEIGHTS)}")
        merged = {**DEFAULT_WEIGHTS, **w}
        total = sum(merged.values())
        if total <= 0:
            raise ValueError("weights must sum to a positive number")
        return {k: v / total for k, v in merged.items()}

    @property
    def slug(self) -> str:
        return self.id or slugify(self.name)

    @property
    def requires_hashtag(self) -> bool:
        return any("hashtag" in r.lower() for r in self.required) and bool(self.hashtags)

    @property
    def requires_subtitles(self) -> bool:
        return any("subtitle" in r.lower() or "caption" in r.lower() for r in self.required)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "campaign"


def parse_yaml(text: str) -> CampaignSpec:
    data = yaml.safe_load(text) or {}
    if isinstance(data, dict) and "campaign" in data and isinstance(data["campaign"], dict):
        data = data["campaign"]
    # The README shape puts cpm under `rate`; accept a flat `cpm` too.
    if "cpm" in data and "rate" not in data:
        data["rate"] = {"cpm": data.pop("cpm")}
    return CampaignSpec.model_validate(data)


def create(spec: CampaignSpec, base_dir: Path | None = None) -> dict[str, Any]:
    """Insert or update a campaign by slug, then register its listed sources."""
    with db.connect() as conn:
        existing = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (spec.slug,)).fetchone()
        values = (spec.name, spec.platform, spec.rate.cpm, spec.minimum_views,
                  spec.maximum_payout, spec.budget, spec.model_dump_json())
        if existing:
            conn.execute(
                "UPDATE campaigns SET name=?, platform=?, cpm=?, min_views=?, max_payout=?, "
                "budget=?, rules_json=? WHERE id=?", (*values, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO campaigns (slug, name, platform, cpm, min_views, max_payout, budget, "
                "rules_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (spec.slug, *values, db.now()))
    campaign = get(spec.slug)
    from .clipping import sources  # local import: sources imports this module

    for src in spec.source:
        path = Path(src).expanduser()
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        if src.startswith(("http://", "https://")) or path.exists():
            sources.add(campaign["slug"], src if src.startswith("http") else str(path))
    return campaign


def get(ref: str | int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE slug = ? OR CAST(id AS TEXT) = ?", (str(ref), str(ref))
        ).fetchone()
    if row is None:
        raise LookupError(f"no campaign '{ref}' (see `clipper campaign list`)")
    return dict(row)


def spec(ref: str | int) -> CampaignSpec:
    return CampaignSpec.model_validate_json(get(ref)["rules_json"])


def list_all() -> list[dict[str, Any]]:
    with db.connect() as conn:
        return db.rows(conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM sources s WHERE s.campaign_id = c.id) AS n_sources, "
            "(SELECT COUNT(*) FROM clips k WHERE k.campaign_id = c.id) AS n_clips "
            "FROM campaigns c ORDER BY c.id"))
