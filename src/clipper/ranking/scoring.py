"""Weighted clip score. The agent scores each dimension; clipper applies the
campaign's weights so the ranking is reproducible and auditable."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .rubric import DIMENSIONS


class ClipScore(BaseModel):
    clip_id: int
    hook: float = Field(ge=0, le=100)
    retention: float = Field(ge=0, le=100)
    context: float = Field(ge=0, le=100)
    emotion: float = Field(ge=0, le=100)
    novelty: float = Field(ge=0, le=100)
    comment: float = Field(ge=0, le=100)
    campaign_fit: float = Field(ge=0, le=100)
    compliant: bool = True
    compliance_notes: str | None = None
    notes: str | None = Field(None, description="Why this clip beats or loses to the others")


def weighted(score: ClipScore, weights: dict[str, float]) -> float:
    return round(sum(getattr(score, dim) * weights[dim] for dim in DIMENSIONS), 1)
