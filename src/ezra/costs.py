"""Approximate processing cost accounting.

Local compute has no invoice, so it is priced from configurable rates
(EZRA_COST_* environment variables, USD). The numbers are estimates and are
labelled as such everywhere they surface; the point is to let the campaign
optimizer weigh expected revenue against what a render or an LLM call costs."""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import func, select

from . import db
from .db.models import CostRecord

DEFAULT_RATES = {
    "compute_per_hour": 0.10,        # CPU-hour for transcription/analysis/render on your machine
    "storage_per_gb_month": 0.023,   # S3-standard-like
    "llm_per_1k_tokens": 0.0,        # subscription CLIs and local models: marginal cost ~0
    "api_call": 0.0,
}


def rate(name: str) -> float:
    return float(os.environ.get(f"EZRA_COST_{name.upper()}", DEFAULT_RATES.get(name, 0.0)))


def record(kind: str, *, quantity: float, unit: str, amount_usd: float | None = None,
           campaign_id: int | None = None, source_id: int | None = None, clip_id: int | None = None,
           **details: Any) -> float:
    if amount_usd is None:
        if unit == "seconds":
            amount_usd = quantity / 3600 * rate("compute_per_hour")
        elif unit == "tokens":
            amount_usd = quantity / 1000 * rate("llm_per_1k_tokens")
        elif unit == "bytes":
            amount_usd = quantity / 1e9 * rate("storage_per_gb_month")
        elif unit == "calls":
            amount_usd = quantity * rate("api_call")
        else:
            amount_usd = 0.0
    with db.session() as s:
        s.add(CostRecord(kind=kind, quantity=quantity, unit=unit, amount_usd=round(amount_usd, 6),
                         campaign_id=campaign_id, source_id=source_id, clip_id=clip_id, details=details))
    return amount_usd


def summary(campaign_id: int | None = None) -> dict[str, Any]:
    with db.session() as s:
        q = select(CostRecord.kind, func.sum(CostRecord.amount_usd), func.sum(CostRecord.quantity),
                   func.min(CostRecord.unit)).group_by(CostRecord.kind)
        if campaign_id is not None:
            q = q.where(CostRecord.campaign_id == campaign_id)
        rows = s.execute(q).all()
    by_kind = {k: {"usd": round(float(a or 0), 4), "quantity": float(qn or 0), "unit": u} for k, a, qn, u in rows}
    return {"total_usd": round(sum(v["usd"] for v in by_kind.values()), 4), "by_kind": by_kind,
            "estimated": True, "rates": {k: rate(k) for k in DEFAULT_RATES}}
