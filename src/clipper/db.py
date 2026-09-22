"""SQLite store. Five core tables (campaigns, sources, clips, posts, metrics)
plus `learnings`, the notes the performance analyst writes back for the judge.

Every call opens its own connection: the MCP server renders and transcribes
on worker threads, and sqlite3 connections must not cross threads.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import ensure_dirs, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id           INTEGER PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    platform     TEXT,
    cpm          REAL NOT NULL DEFAULT 0,
    min_views    INTEGER NOT NULL DEFAULT 0,
    max_payout   REAL,
    budget       REAL,
    rules_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    path            TEXT NOT NULL,
    title           TEXT,
    duration        REAL,
    status          TEXT NOT NULL DEFAULT 'new',
    transcript_path TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (campaign_id, path)
);

CREATE TABLE IF NOT EXISTS clips (
    id                  INTEGER PRIMARY KEY,
    campaign_id         INTEGER NOT NULL REFERENCES campaigns(id),
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    start_time          REAL NOT NULL,
    end_time            REAL NOT NULL,
    title               TEXT,
    hook_text           TEXT,
    hook_type           TEXT,
    topic               TEXT,
    opening_words       TEXT,
    transcript          TEXT,
    rationale           TEXT,
    origin              TEXT NOT NULL DEFAULT 'agent',
    origin_ref          TEXT,
    ai_score            REAL,
    hook_score          REAL,
    retention_score     REAL,
    context_score       REAL,
    emotion_score       REAL,
    novelty_score       REAL,
    comment_score       REAL,
    campaign_fit_score  REAL,
    compliance_score    REAL,
    compliance_issues   TEXT,
    judge_notes         TEXT,
    framing             TEXT,
    video_path          TEXT,
    render_error        TEXT,
    copy_json           TEXT,
    status              TEXT NOT NULL DEFAULT 'candidate',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY,
    clip_id         INTEGER NOT NULL REFERENCES clips(id),
    platform        TEXT NOT NULL,
    post_id         TEXT,
    post_url        TEXT,
    request_id      TEXT,
    status          TEXT NOT NULL DEFAULT 'submitted',
    caption         TEXT,
    actual_payout   REAL,
    response_json   TEXT,
    posted_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY,
    post_id     INTEGER NOT NULL REFERENCES posts(id),
    captured_at TEXT NOT NULL,
    views       INTEGER NOT NULL DEFAULT 0,
    likes       INTEGER,
    comments    INTEGER,
    shares      INTEGER,
    saves       INTEGER,
    origin      TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS learnings (
    id          INTEGER PRIMARY KEY,
    text        TEXT NOT NULL,
    evidence    TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clips_campaign ON clips(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_metrics_post ON metrics(post_id, captured_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    s = settings()
    ensure_dirs(s)
    conn = sqlite3.connect(s.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)
