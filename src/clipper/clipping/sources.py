"""Long-form source footage attached to a campaign."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import campaigns, db
from ..config import settings


def probe_duration(path: str | Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def download(url: str) -> Path:
    """Fetch a URL source with yt-dlp (optional dependency)."""
    if not shutil.which("yt-dlp"):
        raise RuntimeError("URL sources need yt-dlp on PATH (brew install yt-dlp), or download the file yourself")
    out_dir = settings().sources_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
         "-o", str(out_dir / "%(id)s.%(ext)s"), "--print", "after_move:filepath", url],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr.strip()[-500:]}")
    return Path(result.stdout.strip().splitlines()[-1])


def add(campaign_ref: str | int, path_or_url: str, title: str | None = None) -> dict[str, Any]:
    campaign = campaigns.get(campaign_ref)
    if path_or_url.startswith(("http://", "https://")):
        path = download(path_or_url)
    else:
        path = Path(path_or_url).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"source video not found: {path}")
    duration = probe_duration(path)
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sources (campaign_id, path, title, duration, created_at) "
            "VALUES (?,?,?,?,?)",
            (campaign["id"], str(path), title or path.stem, duration, db.now()))
        row = conn.execute(
            "SELECT * FROM sources WHERE campaign_id = ? AND path = ?", (campaign["id"], str(path))
        ).fetchone()
    return dict(row)


def get(source_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise LookupError(f"no source {source_id}")
    return dict(row)


def list_for(campaign_ref: str | int) -> list[dict[str, Any]]:
    campaign = campaigns.get(campaign_ref)
    with db.connect() as conn:
        return db.rows(conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM clips c WHERE c.source_id = s.id) AS n_clips "
            "FROM sources s WHERE campaign_id = ? ORDER BY id", (campaign["id"],)))


def set_status(source_id: int, status: str, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(
            f"UPDATE sources SET status = ?{', ' + cols if cols else ''} WHERE id = ?",
            (status, *fields.values(), source_id))
