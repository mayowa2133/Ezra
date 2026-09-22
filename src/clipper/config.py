"""Runtime settings, all from the environment so the CLI, the MCP server and
headless agent sessions resolve the same data directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    home: Path
    whisper_model: str
    whisper_device: str
    whisper_compute: str
    font_path: str | None
    upload_post_api_key: str | None
    upload_post_user: str | None
    openshorts_url: str
    openshorts_api_key: str | None
    agent: str

    @property
    def db_path(self) -> Path:
        return self.home / "clipper.db"

    @property
    def transcripts_dir(self) -> Path:
        return self.home / "transcripts"

    @property
    def renders_dir(self) -> Path:
        return self.home / "renders"

    @property
    def sources_dir(self) -> Path:
        return self.home / "sources"


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env wins."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def settings() -> Settings:
    load_dotenv(Path.cwd() / ".env")
    home = Path(os.environ.get("CLIPPER_HOME") or Path.cwd() / "data").expanduser().resolve()
    return Settings(
        home=home,
        whisper_model=os.environ.get("CLIPPER_WHISPER_MODEL", "small"),
        whisper_device=os.environ.get("CLIPPER_WHISPER_DEVICE", "auto"),
        whisper_compute=os.environ.get("CLIPPER_WHISPER_COMPUTE", "int8"),
        font_path=os.environ.get("CLIPPER_FONT"),
        upload_post_api_key=os.environ.get("UPLOAD_POST_API_KEY"),
        upload_post_user=os.environ.get("UPLOAD_POST_USER"),
        openshorts_url=os.environ.get("OPENSHORTS_API_URL", "http://localhost:8000").rstrip("/"),
        openshorts_api_key=os.environ.get("OPENSHORTS_API_KEY"),
        agent=os.environ.get("CLIPPER_AGENT", "claude"),
    )


def ensure_dirs(s: Settings) -> None:
    for d in (s.home, s.transcripts_dir, s.renders_dir, s.sources_dir):
        d.mkdir(parents=True, exist_ok=True)
