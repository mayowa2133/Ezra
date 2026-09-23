"""Shared test setup.

Every test runs against its own EZRA_HOME (SQLite + local storage). Media tests
use synthetic fixtures built by ezra.benchmark.fixtures (TTS + a public-domain
portrait), cached in ~/.cache/ezra/fixtures between runs. Whisper runs with the
`base` model in tests for speed; models are cached in ~/.cache/ezra/models.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE_CACHE = Path(os.environ.get("EZRA_FIXTURE_CACHE", Path.home() / ".cache" / "ezra" / "fixtures"))
MODEL_CACHE = Path(os.environ.get("EZRA_TEST_MODEL_CACHE", Path.home() / ".cache" / "ezra" / "models"))


def _reset():
    from ezra import storage
    from ezra.config import reset_settings

    reset_settings()
    storage._storage = None


@pytest.fixture(autouse=True)
def ezra_home(tmp_path, monkeypatch):
    home = tmp_path / "ezra"
    monkeypatch.setenv("EZRA_HOME", str(home))
    for var in ("EZRA_DATABASE_URL", "EZRA_STORAGE", "EZRA_API_TOKEN", "EZRA_LLM", "UPLOAD_POST_API_KEY",
                "EZRA_ALLOW_AUTONOMOUS", "HF_TOKEN", "PEXELS_API_KEY", "EZRA_IMPORT_ROOTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EZRA_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("EZRA_JOB_ISOLATION", "false")
    monkeypatch.setenv("EZRA_WHISPER_MODEL", "base")
    monkeypatch.setenv("EZRA_MODEL_CACHE", str(MODEL_CACHE))
    _reset()
    yield home
    _reset()


def tts_available() -> bool:
    return bool(shutil.which("say") or shutil.which("espeak-ng"))


needs_tts = pytest.mark.skipif(not tts_available(), reason="needs macOS `say` or espeak-ng to build fixtures")


@pytest.fixture(scope="session")
def fixture_video():
    """Build (or reuse) a benchmark fixture by name."""
    from ezra.benchmark import fixtures

    def get(name: str) -> tuple[Path, Path]:
        if not tts_available():
            pytest.skip("no TTS engine for fixtures")
        return fixtures.build(name, FIXTURE_CACHE)

    return get


@pytest.fixture
def tiny_video(tmp_path) -> Path:
    """12s test pattern with a tone: for tests that need valid media, not speech."""
    p = tmp_path / "tiny.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "12",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


DEMO_YAML = """
campaign:
  id: demo
  name: Founder Stories
  rate: {cpm: 2.0}
  minimum_views: 5000
  maximum_payout: 1000
  budget: 1500
  tracking_window_days: 30
  requirements: {min_duration: 15, max_duration: 60}
  forbidden: [profanity, competitor mentions]
  competitors: [Acme Ventures]
  hashtags: ["#founderstories"]
  platforms: [tiktok, instagram, youtube]
  brief: Founder stories with stakes - money lost, near quitting, contrarian fundraising takes.
"""


@pytest.fixture(scope="session")
def processed_home(tmp_path_factory, fixture_video):
    """One real end-to-end processed campaign (podcast fixture): analyzed + candidates.
    Tests copy it into their own EZRA_HOME with `use_processed`."""
    video, truth = fixture_video("podcast")
    home = tmp_path_factory.mktemp("processed") / "ezra"
    mp = pytest.MonkeyPatch()
    mp.setenv("EZRA_HOME", str(home))
    mp.setenv("EZRA_SECRET_KEY", "test-secret-key")
    mp.setenv("EZRA_JOB_ISOLATION", "false")
    mp.setenv("EZRA_WHISPER_MODEL", "base")
    mp.setenv("EZRA_MODEL_CACHE", str(MODEL_CACHE))
    _reset()
    from ezra import campaigns, candidates, sources

    campaigns.import_text(DEMO_YAML)
    src = sources.ingest(video, "demo", rights_basis="campaign_supplied")
    candidates.find_candidates(src.id, "demo")
    from ezra import db

    db.reset_engine()
    mp.undo()
    _reset()
    return {"home": home, "video": video, "truth": truth}


@pytest.fixture
def use_processed(processed_home, ezra_home, monkeypatch):
    """Copy the processed campaign into this test's EZRA_HOME."""
    shutil.copytree(processed_home["home"], ezra_home, dirs_exist_ok=True)
    _reset()
    return processed_home
