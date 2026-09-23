"""The production backends: PostgreSQL (migrations, SKIP LOCKED job claiming)
and an S3-compatible store (MinIO). Skipped unless the docker-compose `test`
profile provides them:

    docker compose --profile test run --rm test pytest -q tests/test_postgres_s3.py
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import pytest

PG = os.environ.get("EZRA_TEST_DATABASE_URL")
S3 = os.environ.get("EZRA_TEST_S3_ENDPOINT")

pytestmark = pytest.mark.skipif(not PG, reason="set EZRA_TEST_DATABASE_URL (docker compose --profile test)")


@pytest.fixture
def pg(monkeypatch, ezra_home):
    """A fresh database per test, created from the server's maintenance DB."""
    import sqlalchemy as sa

    from ezra.config import reset_settings

    base, _, _ = PG.rpartition("/")
    name = f"ezra_t_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(sa.text(f'CREATE DATABASE "{name}"'))
    monkeypatch.setenv("EZRA_DATABASE_URL", f"{base}/{name}")
    if S3:
        monkeypatch.setenv("EZRA_STORAGE", "s3")
        monkeypatch.setenv("EZRA_S3_ENDPOINT", S3)
        monkeypatch.setenv("EZRA_S3_BUCKET", name.replace("_", "-"))
    reset_settings()
    from ezra import db, storage

    storage._storage = None
    db.migrate()
    yield name
    db.reset_engine()
    with admin.connect() as c:
        c.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def test_migrations_create_every_table(pg):
    import sqlalchemy as sa

    from ezra import db
    from ezra.db.models import Base

    names = set(sa.inspect(db.engine()).get_table_names())
    assert set(Base.metadata.tables) <= names and "alembic_version" in names
    db.migrate()   # idempotent


def test_skip_locked_claims_each_job_once(pg):
    from ezra import jobs

    ids = [jobs.enqueue("noop", {}).id for _ in range(40)]
    claimed: list[int] = []
    lock = threading.Lock()

    def worker():
        while (j := jobs.claim()) is not None:
            with lock:
                claimed.append(j.id)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)          # every job, none twice


def test_campaign_source_and_job_on_postgres_and_s3(pg, tiny_video):
    from ezra import campaigns, jobs, sources, storage
    from tests.conftest import DEMO_YAML

    camp = campaigns.import_text(DEMO_YAML)[0]
    assert campaigns.get("demo").id == camp.id
    src = sources.ingest(tiny_video, "demo", rights_basis="campaign_supplied")
    st = storage.get_storage()
    assert st.name == ("s3" if S3 else "local")
    assert st.exists(src.storage_key) and st.size(src.storage_key) == tiny_video.stat().st_size
    again = sources.ingest(tiny_video, "demo")                    # sha256 dedupe
    assert again.id == src.id
    local = sources.local_path(sources.get(src.id))
    assert Path(local).read_bytes() == tiny_video.read_bytes()
    job = jobs.enqueue("noop", {"x": 1})
    done = jobs.run(jobs.claim(job.id))
    assert done.status == "completed"


@pytest.mark.skipif(not S3, reason="needs EZRA_TEST_S3_ENDPOINT")
def test_s3_rejects_traversal_keys(pg):
    from ezra import storage

    st = storage.get_storage()
    with pytest.raises(storage.StorageError):
        st.put_bytes("../escape.txt", b"x")
    key = st.put_bytes("probe/a.txt", b"hello")
    assert list(st.list("probe/")) == [key] and st.local_path(key).read_bytes() == b"hello"
    st.delete(key)
    assert not st.exists(key)


def test_full_pipeline_on_postgres_and_s3(pg, fixture_video):
    """Analyze, find candidates, render and approve with the production backends."""
    from ezra import campaigns, candidates, render, review, sources, storage
    from tests.conftest import DEMO_YAML

    video, _ = fixture_video("solo")
    campaigns.import_text(DEMO_YAML)
    src = sources.ingest(video, "demo", rights_basis="campaign_supplied")
    candidates.find_candidates(src.id, "demo")
    top = candidates.list_candidates("demo", top=1)
    assert top and top[0].rank_score is not None
    clip = render.render_candidate(top[0].id)
    v = render.current_version(clip)
    assert (v.width, v.height) == (1080, 1920) and storage.get_storage().exists(v.video_key)
    review.approve(clip.id, actor="test")
    assert render.get_clip(clip.id).status == "approved"
