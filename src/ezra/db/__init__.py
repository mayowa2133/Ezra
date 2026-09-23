"""Engine, sessions and migrations. One engine per process; every unit of work
uses `session()` so API requests, worker jobs and CLI commands share one
transaction discipline."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None
_migrated: set[str] = set()
_lock = threading.Lock()


def engine() -> Engine:
    global _engine, _factory
    with _lock:
        if _engine is None:
            url = get_settings().db_url
            if url.startswith("sqlite"):
                _engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

                @event.listens_for(_engine, "connect")
                def _sqlite_pragmas(dbapi_conn, _record):
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA foreign_keys = ON")
                    cur.execute("PRAGMA journal_mode = WAL")
                    cur.close()
            else:
                _engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)
            _factory = sessionmaker(_engine, expire_on_commit=False)
        return _engine


def reset_engine() -> None:
    global _engine, _factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _factory = None


def migrate() -> None:
    """Apply Alembic migrations up to head (idempotent, once per process per URL)."""
    url = get_settings().db_url
    if url in _migrated:
        return
    import logging

    from alembic import command

    logging.getLogger("alembic").setLevel(logging.WARNING)

    cfg = alembic_config(url)
    with engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    _migrated.add(url)


def alembic_config(url: str | None = None) -> Any:
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url or get_settings().db_url)
    return cfg


def revision(message: str) -> str:
    """Autogenerate a migration from the models against the current (migrated) database."""
    from alembic import command

    migrate()
    cfg = alembic_config()
    with engine().begin() as conn:
        cfg.attributes["connection"] = conn
        script = command.revision(cfg, message=message, autogenerate=True)
    return str(getattr(script, "path", script))


@contextmanager
def session() -> Iterator[Session]:
    migrate()
    engine()
    assert _factory is not None
    s = _factory()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything in Ezra is UTC-aware."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
