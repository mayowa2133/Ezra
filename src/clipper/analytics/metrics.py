"""Performance snapshots. Each record is a point-in-time reading, so views at
1h / 24h / 7d after posting can be derived later instead of overwritten."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .. import db


def record(post_id: int, views: int, likes: int | None = None, comments: int | None = None,
           shares: int | None = None, saves: int | None = None, origin: str = "manual",
           captured_at: str | None = None) -> dict[str, Any]:
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone() is None:
            raise LookupError(f"no post {post_id} (see `clipper posts`)")
        cur = conn.execute(
            "INSERT INTO metrics (post_id, captured_at, views, likes, comments, shares, saves, origin) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (post_id, captured_at or db.now(), views, likes, comments, shares, saves, origin))
        return dict(conn.execute("SELECT * FROM metrics WHERE id = ?", (cur.lastrowid,)).fetchone())


def set_payout(post_id: int, amount: float) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE posts SET actual_payout = ? WHERE id = ?", (amount, post_id))


def views_at(post: dict[str, Any], hours: float) -> int | None:
    """Views from the last snapshot taken within `hours` of posting."""
    cutoff = datetime.fromisoformat(post["posted_at"]) + timedelta(hours=hours)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT views FROM metrics WHERE post_id = ? AND captured_at <= ? "
            "ORDER BY captured_at DESC LIMIT 1", (post["id"], cutoff.isoformat(timespec="seconds"))
        ).fetchone()
    return row["views"] if row else None
