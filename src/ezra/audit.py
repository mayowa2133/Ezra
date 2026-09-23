"""Audit trail for consequential actions (approvals, publishing, credentials)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from . import db
from .db.models import AuditEvent


def record(action: str, entity_type: str, entity_id: Any = None, actor: str = "system",
           **details: Any) -> None:
    with db.session() as s:
        s.add(AuditEvent(action=action, entity_type=entity_type,
                         entity_id=None if entity_id is None else str(entity_id),
                         actor=actor, details=details))


def recent(limit: int = 100, entity_type: str | None = None) -> list[AuditEvent]:
    with db.session() as s:
        q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
        if entity_type:
            q = q.where(AuditEvent.entity_type == entity_type)
        return list(s.scalars(q))
