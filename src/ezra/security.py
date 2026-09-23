"""Input validation and request-level safeguards shared by API, CLI and MCP."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as pysecrets
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select

from . import db
from .config import get_settings
from .db.models import OAuthState, utcnow

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mpeg", ".mpg", ".ts"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class ValidationError(ValueError):
    pass


def probe(path: Path) -> dict:
    """ffprobe the file; raises ValidationError if it is not readable media."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration,format_name,size:stream=codec_type,width,height,avg_frame_rate",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as e:
        raise ValidationError(f"{path.name}: probing timed out") from e
    if out.returncode != 0:
        raise ValidationError(f"{path.name} is not a readable media file: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def validate_media(path: Path, kind: str = "video") -> dict:
    """Extension allow-list + size limit + container/stream check via ffprobe.
    Returns normalized metadata."""
    path = Path(path)
    if not path.is_file():
        raise ValidationError(f"not a file: {path}")
    allowed = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS if kind == "video" else IMAGE_EXTENSIONS
    if path.suffix.lower() not in allowed:
        raise ValidationError(f"{path.name}: unsupported type {path.suffix!r} (allowed: {', '.join(sorted(allowed))})")
    limit = get_settings().max_upload_mb * 1024 * 1024
    size = path.stat().st_size
    if size > limit:
        raise ValidationError(f"{path.name}: {size / 1e6:.0f} MB exceeds the {get_settings().max_upload_mb} MB limit")
    if kind != "video":
        return {"size": size}
    info = probe(path)
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        raise ValidationError(f"{path.name} has no audio or video streams")
    duration = float((info.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise ValidationError(f"{path.name}: could not determine duration")
    fps = None
    if video and video.get("avg_frame_rate") not in (None, "0/0"):
        n, d = video["avg_frame_rate"].split("/")
        fps = float(n) / float(d) if float(d) else None
    return {"size": size, "duration": duration, "has_audio": audio is not None,
            "width": video.get("width") if video else None, "height": video.get("height") if video else None,
            "fps": fps, "format": (info.get("format") or {}).get("format_name")}


def allowed_import_roots() -> list[Path]:
    raw = os.environ.get("EZRA_IMPORT_ROOTS")
    if raw:
        return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p]
    return [Path.cwd().resolve(), get_settings().home.resolve()]


def safe_import_path(path: str | Path, roots: list[Path] | None = None) -> Path:
    """For server-side imports requested over the API/MCP: the path must resolve
    inside an allowed root (no traversal, no following symlinks out)."""
    p = Path(path).expanduser().resolve()
    for root in roots or allowed_import_roots():
        if p == root or root in p.parents:
            return p
    raise PermissionError(f"{path} is outside the allowed import directories (EZRA_IMPORT_ROOTS)")


# --- webhooks ------------------------------------------------------------------

def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = timestamp or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(body: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected = sign(body, secret, ts).split("v1=")[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


# --- OAuth state & redirects ---------------------------------------------------

OAUTH_STATE_TTL = timedelta(minutes=10)


def new_oauth_state(provider: str, account_label: str | None = None,
                    code_verifier: str | None = None) -> str:
    state = pysecrets.token_urlsafe(32)
    with db.session() as s:
        s.add(OAuthState(state=state, provider=provider, account_label=account_label,
                         code_verifier=code_verifier))
    return state


def consume_oauth_state(state: str, provider: str) -> OAuthState:
    """Single-use, provider-bound, time-limited."""
    with db.session() as s:
        row = s.scalar(select(OAuthState).where(OAuthState.state == state))
        if row is None or row.provider != provider:
            raise PermissionError("unknown OAuth state")
        if row.used:
            raise PermissionError("OAuth state already used")
        if utcnow() - (db.aware(row.created_at) or utcnow()) > OAUTH_STATE_TTL:
            raise PermissionError("OAuth state expired")
        row.used = True
        return row


def safe_redirect(target: str | None) -> str:
    """Only same-app redirects: relative paths or the configured web origin."""
    web = get_settings().web_url.rstrip("/")
    if not target:
        return web
    parsed = urlparse(target)
    if not parsed.scheme and not parsed.netloc and target.startswith("/") and not target.startswith("//"):
        return web + target
    if f"{parsed.scheme}://{parsed.netloc}" == web:
        return target
    return web


# --- rate limiting -------------------------------------------------------------

class RateLimiter:
    """Token bucket per key (client IP)."""

    def __init__(self, per_minute: int):
        self.rate = per_minute / 60.0
        self.capacity = float(per_minute)
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1:
                self.buckets[key] = (tokens, now)
                return False
            self.buckets[key] = (tokens - 1, now)
            return True
