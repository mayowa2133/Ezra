"""Runtime settings. Everything comes from the environment (or a .env file in
the working directory) so the API, worker, CLI and MCP server resolve the same
database, storage and providers. Secrets are read here but never persisted to
ordinary tables; see ezra.secrets."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EZRA_", env_file=".env", extra="ignore")

    # --- core -------------------------------------------------------------
    home: Path = Field(default_factory=lambda: Path.cwd() / "data",
                       description="Local working directory: SQLite DB, local storage, caches")
    database_url: str | None = Field(None, description="SQLAlchemy URL; default sqlite in EZRA_HOME")
    storage: str = Field("local", description="local | s3")
    s3_endpoint: str | None = None
    s3_bucket: str = "ezra"
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # --- API / security ---------------------------------------------------
    api_token: str | None = Field(None, description="Bearer token required by the API when set")
    public_url: str = "http://localhost:8000"
    web_url: str = "http://localhost:3000"
    secret_key: str | None = Field(None, description="Key for the encrypted secret store and signing")
    max_upload_mb: int = 4096
    rate_limit_per_minute: int = 240

    # --- providers ----------------------------------------------------------
    transcriber: str = Field("faster-whisper", description="faster-whisper | whisperx")
    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute: str = "int8"
    whisper_batch: int = 8
    whisper_prompt: str | None = Field(None, description="Initial prompt, e.g. 'Um, uh, like' to keep disfluencies")
    model_cache: Path | None = Field(None, description="Where ML models are cached; default $EZRA_HOME/cache/models")
    diarizer: str = Field("local", description="local | pyannote | none")
    face_detector: str = Field("auto", description="auto (YuNet, Haar offline) | yunet | haar | mediapipe")
    llm: str = Field("heuristic", description="heuristic | claude-cli | codex-cli | openai-compatible")
    llm_model: str | None = None
    llm_base_url: str | None = None
    clip_engine: str = Field("native", description="native | openshorts")
    font_path: str | None = None
    broll_library: Path | None = None

    # --- external services (credentials live in env / secret store) -------
    openshorts_url: str = "http://localhost:8001"

    # --- worker -------------------------------------------------------------
    worker_poll_seconds: float = 1.0
    job_max_attempts: int = 3
    job_isolation: bool = Field(True, description="Run each job in a child process")

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.home / 'ezra.db'}"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def models_dir(self) -> Path:
        return (self.model_cache or self.cache_dir / "models").expanduser()

    @property
    def storage_dir(self) -> Path:
        return self.home / "storage"

    @property
    def work_dir(self) -> Path:
        return self.home / "work"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.cache_dir, self.storage_dir, self.work_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.home = s.home.expanduser().resolve()
    s.ensure_dirs()
    return s


def reset_settings() -> None:
    """Tests and CLIs that change EZRA_* env vars call this to re-read them."""
    get_settings.cache_clear()
    from . import db

    db.reset_engine()
