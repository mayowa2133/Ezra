from datetime import timedelta

import pytest
from sqlalchemy import inspect

from ezra import db, jobs, secrets, storage, worker
from ezra.db.models import Job, utcnow


def test_migrations_create_every_table():
    db.migrate()
    names = set(inspect(db.engine()).get_table_names())
    for t in ("campaigns", "campaign_rules", "sources", "transcripts", "transcript_segments", "candidates",
              "clips", "clip_versions", "brand_kits", "publish_accounts", "posts", "metric_snapshots",
              "experiments", "revenue_records", "cost_records", "jobs", "job_logs",
              "integration_credentials", "audit_events", "source_analyses", "learnings", "alembic_version"):
        assert t in names, t


def test_job_lifecycle_progress_and_logs():
    job = jobs.enqueue("noop", {"echo": "hi"})
    assert job.status == "queued"
    done = worker.run_one()
    assert done.status == "completed" and done.result == {"ok": True, "echo": "hi"} and done.progress == 1.0
    messages = [x.message for x in jobs.logs(job.id)]
    assert messages[0] == "queued noop" and messages[-1] == "completed"


def test_job_retry_with_backoff_then_success(tmp_path):
    marker = tmp_path / "count"
    job = jobs.enqueue("noop", {"fail_times": 2, "marker": str(marker)}, max_attempts=3)
    first = worker.run_one()
    assert first.status == "queued" and first.error["type"] == "RuntimeError" and "retrying" in first.message
    with db.session() as s:  # skip the backoff wait
        s.get(Job, job.id).run_after = utcnow() - timedelta(seconds=1)
    worker.run_one()
    with db.session() as s:
        s.get(Job, job.id).run_after = utcnow() - timedelta(seconds=1)
    third = worker.run_one()
    assert third.status == "completed" and third.attempts == 3


def test_job_fails_permanently_after_max_attempts(tmp_path):
    job = jobs.enqueue("noop", {"fail_times": 5, "marker": str(tmp_path / "c")}, max_attempts=1)
    out = worker.run_one()
    assert out.status == "failed" and "planned failure" in out.error["message"] and out.error["traceback"]
    again = jobs.retry(job.id)
    assert again.status == "queued" and again.attempts == 0


def test_unknown_kind_and_bad_retry_are_rejected():
    with pytest.raises(ValueError):
        jobs.enqueue("nope")
    job = jobs.enqueue("noop")
    with pytest.raises(ValueError):
        jobs.retry(job.id)


def test_cancel_queued_and_dedupe():
    a = jobs.enqueue("noop", dedupe_key="src-1")
    b = jobs.enqueue("noop", dedupe_key="src-1")
    assert a.id == b.id
    assert jobs.cancel(a.id).status == "cancelled"
    assert worker.run_one() is None
    c = jobs.enqueue("noop", dedupe_key="src-1")
    assert c.id != a.id


def test_stale_running_job_is_requeued():
    job = jobs.enqueue("noop")
    jobs.claim(job.id)
    with db.session() as s:
        s.get(Job, job.id).heartbeat_at = utcnow() - timedelta(minutes=5)
    assert jobs.requeue_stale() == [job.id]
    assert jobs.get(job.id).status == "queued"


def test_isolated_worker_runs_and_cancels_child_process(monkeypatch, ezra_home):
    monkeypatch.setenv("EZRA_JOB_ISOLATION", "true")
    from ezra.config import reset_settings
    reset_settings()
    ok = jobs.enqueue("noop", {"echo": "child"})
    assert worker.run_one().status == "completed"
    assert jobs.get(ok.id).result["echo"] == "child"

    import threading
    slow = jobs.enqueue("noop", {"sleep": 30})
    t = threading.Timer(3.0, lambda: jobs.cancel(slow.id))
    t.start()
    out = worker.run_one()
    t.join()
    assert out.status == "cancelled"


def test_storage_roundtrip_and_traversal_guard(tmp_path):
    st = storage.get_storage()
    f = tmp_path / "a.txt"
    f.write_text("hello")
    key = st.put_file("sources/1/a.txt", f)
    assert st.exists(key) and st.local_path(key).read_text() == "hello" and st.size(key) == 5
    assert list(st.list("sources")) == ["sources/1/a.txt"]
    for bad in ("../x", "/etc/passwd", "a/../../b", "a\\b", ""):
        with pytest.raises(storage.StorageError):
            st.put_bytes(bad, b"x")


def test_s3_storage_with_moto(tmp_path):
    moto = pytest.importorskip("moto")
    import boto3
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        st = storage.S3Storage("ezra-test", None, "us-east-1", "k", "s", tmp_path, client=client)
        st.put_bytes("clips/1/v1.mp4", b"video")
        assert st.exists("clips/1/v1.mp4") and st.size("clips/1/v1.mp4") == 5
        assert st.local_path("clips/1/v1.mp4").read_bytes() == b"video"
        assert "clips/1/v1.mp4" in st.url("clips/1/v1.mp4")


def test_secrets_are_encrypted_and_env_wins(monkeypatch, ezra_home):
    secrets.put("youtube:me", {"refresh_token": "abc"})
    raw = (ezra_home / "secrets.enc").read_bytes()
    assert b"abc" not in raw
    assert secrets.get("youtube:me") == {"refresh_token": "abc"}
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "from-env")
    assert secrets.get("upload-post") == "from-env"
    with pytest.raises(secrets.SecretError):
        secrets.require("pexels", "B-roll search")


def test_migrations_match_the_models():
    """An autogenerate against a freshly migrated DB finds nothing: models and migrations agree."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from ezra import db
    from ezra.db.models import Base

    db.migrate()
    with db.engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff
