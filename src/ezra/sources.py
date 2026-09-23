"""Source ingestion. Ezra assumes media needs authorization: every source
carries rights metadata, and compliance treats unverified rights as
REVIEW_REQUIRED and rejected rights as FAIL. Campaign-supplied and
user-owned footage is the expected input; URL download (yt-dlp) is opt-in and
records the origin URL for review."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from . import audit, campaigns, db, security
from .config import get_settings
from .db.models import Candidate, Source
from .storage import get_storage, sha256_file

RIGHTS_BASES = {"campaign_supplied", "owned", "licensed", "permission", "unknown"}


def download(url: str, dest_dir: Path) -> Path:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("URL sources need yt-dlp on PATH; or download the file and add it by path")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(
        ["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
         "-o", str(dest_dir / "%(id)s.%(ext)s"), "--print", "after_move:filepath", url],
        capture_output=True, text=True, timeout=3600)
    if out.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {out.stderr.strip()[-500:]}")
    return Path(out.stdout.strip().splitlines()[-1])


def ingest(path_or_url: str | Path, campaign: str | int | None = None, title: str | None = None,
           rights_basis: str = "unknown", rights_notes: str | None = None, actor: str = "user") -> Source:
    """Validate, fingerprint and store a source. Re-adding the same file to the
    same campaign returns the existing source (sha256 dedupe)."""
    if rights_basis not in RIGHTS_BASES:
        raise ValueError(f"rights_basis must be one of {sorted(RIGHTS_BASES)}")
    origin_url = None
    if isinstance(path_or_url, str) and path_or_url.startswith(("http://", "https://")):
        origin_url = path_or_url
        path = download(path_or_url, get_settings().work_dir / "downloads")
    else:
        path = Path(path_or_url).expanduser().resolve()
    meta = security.validate_media(path, "video")
    camp = campaigns.get(campaign) if campaign is not None else None
    digest = sha256_file(path)
    with db.session() as s:
        existing = s.scalar(select(Source).where(Source.sha256 == digest,
                                                 Source.campaign_id == (camp.id if camp else None)))
        if existing:
            return existing
        src = Source(campaign_id=camp.id if camp else None, title=title or path.stem,
                     storage_key="pending", original_filename=path.name, origin_url=origin_url,
                     sha256=digest, mime=meta.get("format"), size_bytes=meta["size"], duration=meta["duration"],
                     width=meta.get("width"), height=meta.get("height"), fps=meta.get("fps"),
                     has_audio=meta.get("has_audio", True), rights_basis=rights_basis, rights_notes=rights_notes,
                     rights_status="authorized" if rights_basis in ("campaign_supplied", "owned", "licensed",
                                                                    "permission") else "unverified")
        s.add(src)
        s.flush()
        src.storage_key = get_storage().put_file(f"sources/{src.id}/original{path.suffix.lower()}", path)
        source_id = src.id
    audit.record("source.ingested", "source", source_id, actor=actor, sha256=digest,
                 rights_basis=rights_basis, origin_url=origin_url)
    return get(source_id)


def set_rights(source_id: int, status: str, basis: str | None = None, notes: str | None = None,
               actor: str = "user") -> Source:
    if status not in ("authorized", "unverified", "rejected"):
        raise ValueError("status must be authorized | unverified | rejected")
    with db.session() as s:
        src = s.get(Source, source_id)
        if src is None:
            raise LookupError(f"no source {source_id}")
        src.rights_status = status
        if basis:
            src.rights_basis = basis
        if notes:
            src.rights_notes = notes
    audit.record("source.rights", "source", source_id, actor=actor, status=status, basis=basis)
    return get(source_id)


def get(source_id: int) -> Source:
    with db.session() as s:
        src = s.get(Source, source_id)
        if src is None:
            raise LookupError(f"no source {source_id}")
        return src


def list_sources(campaign: str | int | None = None) -> list[Source]:
    with db.session() as s:
        q = select(Source).order_by(Source.id)
        if campaign is not None:
            q = q.where(Source.campaign_id == campaigns.get(campaign).id)
        return list(s.scalars(q))


def local_path(src: Source) -> Path:
    return get_storage().local_path(src.storage_key)


def set_status(source_id: int, status: str) -> None:
    with db.session() as s:
        src = s.get(Source, source_id)
        if src is not None:
            src.status = status


def to_dict(src: Source) -> dict[str, Any]:
    with db.session() as s:
        n = s.scalar(select(func.count()).select_from(Candidate).where(Candidate.source_id == src.id))
    return {"id": src.id, "campaign_id": src.campaign_id, "title": src.title, "duration": src.duration,
            "width": src.width, "height": src.height, "fps": src.fps, "has_audio": src.has_audio,
            "size_bytes": src.size_bytes, "sha256": src.sha256, "status": src.status,
            "rights_status": src.rights_status, "rights_basis": src.rights_basis,
            "rights_notes": src.rights_notes, "origin_url": src.origin_url, "storage_key": src.storage_key,
            "n_candidates": n, "created_at": src.created_at.isoformat() if src.created_at else None}
