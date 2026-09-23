import pytest


@pytest.fixture(autouse=True)
def ezra_home(tmp_path, monkeypatch):
    """Every test gets its own EZRA_HOME (SQLite DB + local storage)."""
    monkeypatch.setenv("EZRA_HOME", str(tmp_path / "ezra"))
    for var in ("EZRA_DATABASE_URL", "EZRA_STORAGE", "EZRA_API_TOKEN", "EZRA_LLM", "UPLOAD_POST_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EZRA_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("EZRA_JOB_ISOLATION", "false")
    from ezra.config import reset_settings
    reset_settings()
    yield tmp_path / "ezra"
    reset_settings()
