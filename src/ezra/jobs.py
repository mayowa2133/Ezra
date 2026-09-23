"""Durable job queue on the application database.

Why not Redis/Celery: every job must expose progress, logs, structured errors,
retries and cancellation to the API, CLI, MCP and dashboard, which means a jobs
table exists anyway. Claiming with `FOR UPDATE SKIP LOCKED` (PostgreSQL) or a
conditional UPDATE (SQLite) makes that table the queue, one fewer service to run.

Lifecycle: queued → running → completed | failed | cancelled.
A failed attempt is re-queued with backoff until max_attempts. A running job
whose worker stops heartbeating is re-queued (resumable: tasks checkpoint
their expensive outputs, so a rerun skips finished steps).
"""

from __future__ import annotations

import os
import socket
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from . import db
from .db.models import Job, JobLog, utcnow

TaskFn = Callable[["JobContext"], dict[str, Any] | None]
_registry: dict[str, TaskFn] = {}

ACTIVE = ("queued", "running")
STALE_AFTER = timedelta(seconds=90)


class Cancelled(Exception):
    pass


class RetryableError(Exception):
    """Raise for transient failures (network, rate limits) worth another attempt."""


def task(kind: str) -> Callable[[TaskFn], TaskFn]:
    def register(fn: TaskFn) -> TaskFn:
        _registry[kind] = fn
        return fn
    return register


def registry() -> dict[str, TaskFn]:
    from . import tasks  # noqa: F401  (registers the built-in tasks)

    return _registry


@dataclass
class JobContext:
    job_id: int
    payload: dict[str, Any]

    def progress(self, fraction: float, message: str | None = None) -> None:
        with db.session() as s:
            job = s.get(Job, self.job_id)
            assert job is not None
            if job.cancel_requested:
                raise Cancelled()
            job.progress = max(0.0, min(1.0, fraction))
            if message:
                job.message = message
            job.heartbeat_at = utcnow()

    def log(self, message: str, level: str = "info") -> None:
        with db.session() as s:
            s.add(JobLog(job_id=self.job_id, level=level, message=message[:4000]))

    def check_cancelled(self) -> None:
        with db.session() as s:
            job = s.get(Job, self.job_id)
            if job is not None and job.cancel_requested:
                raise Cancelled()


def enqueue(kind: str, payload: dict[str, Any] | None = None, *, priority: int = 100,
            dedupe_key: str | None = None, max_attempts: int | None = None,
            parent_id: int | None = None, run_after: datetime | None = None) -> Job:
    """Queue a job. With a dedupe_key, an identical queued/running job is returned
    instead of creating a duplicate (e.g. two `analyze` calls for one source)."""
    from .config import get_settings

    if kind not in registry():
        raise ValueError(f"unknown job kind {kind!r}")
    with db.session() as s:
        if dedupe_key:
            existing = s.scalar(select(Job).where(Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE)))
            if existing:
                return existing
        job = Job(kind=kind, payload=payload or {}, priority=priority, dedupe_key=dedupe_key,
                  max_attempts=max_attempts or get_settings().job_max_attempts, parent_id=parent_id,
                  run_after=run_after or utcnow())
        s.add(job)
        s.flush()
        s.add(JobLog(job_id=job.id, message=f"queued {kind}"))
        return job


def get(job_id: int) -> Job:
    with db.session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise LookupError(f"no job {job_id}")
        return job


def logs(job_id: int, limit: int = 200) -> list[JobLog]:
    with db.session() as s:
        return list(s.scalars(select(JobLog).where(JobLog.job_id == job_id)
                              .order_by(JobLog.id.desc()).limit(limit)))[::-1]


def list_jobs(status: str | None = None, kind: str | None = None, limit: int = 50) -> list[Job]:
    with db.session() as s:
        q = select(Job).order_by(Job.id.desc()).limit(limit)
        if status:
            q = q.where(Job.status == status)
        if kind:
            q = q.where(Job.kind == kind)
        return list(s.scalars(q))


def _worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def claim(job_id: int | None = None, kinds: list[str] | None = None) -> Job | None:
    """Atomically move one due queued job (or a specific one) to running."""
    now = utcnow()
    with db.session() as s:
        dialect = s.get_bind().dialect.name
        q = select(Job.id).where(Job.status == "queued", Job.run_after <= now)
        if job_id is not None:
            q = q.where(Job.id == job_id)
        if kinds:
            q = q.where(Job.kind.in_(kinds))
        q = q.order_by(Job.priority, Job.id).limit(1)
        if dialect == "postgresql":
            q = q.with_for_update(skip_locked=True)
        picked = s.scalar(q)
        if picked is None:
            return None
        res = s.execute(update(Job).where(Job.id == picked, Job.status == "queued").values(
            status="running", started_at=now, heartbeat_at=now, worker=_worker_name(),
            attempts=Job.attempts + 1, message=None))
        if getattr(res, "rowcount", 1) != 1:  # lost the race (SQLite path)
            return None
        s.add(JobLog(job_id=picked, message=f"started by {_worker_name()}"))
    return get(picked)


def run(job: Job) -> Job:
    """Execute a claimed job in this process and record the outcome."""
    fn = registry().get(job.kind)
    ctx = JobContext(job.id, job.payload or {})
    try:
        if fn is None:
            raise ValueError(f"no task registered for {job.kind!r}")
        result = fn(ctx) or {}
    except Cancelled:
        _finish(job.id, "cancelled", message="cancelled")
    except Exception as e:  # recorded, not swallowed: surfaces in job.error and logs
        _record_failure(job.id, e)
    else:
        _finish(job.id, "completed", result=result, progress=1.0)
    return get(job.id)


def _finish(job_id: int, status: str, result: dict[str, Any] | None = None,
            message: str | None = None, progress: float | None = None) -> None:
    with db.session() as s:
        job = s.get(Job, job_id)
        assert job is not None
        job.status = status
        job.finished_at = utcnow()
        if result is not None:
            job.result = result
        if message:
            job.message = message
        if progress is not None:
            job.progress = progress
        s.add(JobLog(job_id=job_id, message=f"{status}"))


def _record_failure(job_id: int, exc: BaseException) -> None:
    with db.session() as s:
        job = s.get(Job, job_id)
        assert job is not None
        error: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)[:4000],
                                 "traceback": traceback.format_exc()[-6000:], "attempt": job.attempts}
        job.error = error
        permanent = isinstance(exc, (ValueError, LookupError, PermissionError))
        if not permanent and job.attempts < job.max_attempts:
            delay = min(300, 5 * 2 ** (job.attempts - 1))
            job.status = "queued"
            job.run_after = utcnow() + timedelta(seconds=delay)
            job.message = f"attempt {job.attempts} failed ({error['type']}); retrying in {delay}s"
            s.add(JobLog(job_id=job_id, level="warning", message=f"{error['type']}: {error['message']}"))
        else:
            job.status = "failed"
            job.finished_at = utcnow()
            job.message = f"{error['type']}: {error['message'][:300]}"
            s.add(JobLog(job_id=job_id, level="error", message=f"{error['type']}: {error['message']}"))


def cancel(job_id: int) -> Job:
    with db.session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise LookupError(f"no job {job_id}")
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = utcnow()
        elif job.status == "running":
            job.cancel_requested = True
        s.add(JobLog(job_id=job_id, message="cancel requested"))
    return get(job_id)


def retry(job_id: int) -> Job:
    """Re-queue a failed or cancelled job (the retry button)."""
    with db.session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise LookupError(f"no job {job_id}")
        if job.status not in ("failed", "cancelled"):
            raise ValueError(f"job {job_id} is {job.status}; only failed/cancelled jobs can be retried")
        job.status = "queued"
        job.attempts = 0
        job.cancel_requested = False
        job.run_after = utcnow()
        job.finished_at = None
        s.add(JobLog(job_id=job_id, message="retry requested"))
    return get(job_id)


def requeue_stale(now: datetime | None = None) -> list[int]:
    """Running jobs whose worker vanished go back to the queue."""
    now = now or utcnow()
    with db.session() as s:
        stale = list(s.scalars(select(Job).where(Job.status == "running")))
        out = []
        for job in stale:
            hb = db.aware(job.heartbeat_at) or db.aware(job.started_at) or now
            if now - hb > STALE_AFTER:
                job.status = "queued" if job.attempts < job.max_attempts else "failed"
                job.message = "worker stopped responding; " + ("re-queued" if job.status == "queued" else "gave up")
                s.add(JobLog(job_id=job.id, level="warning", message=job.message))
                out.append(job.id)
        return out


def wait(job_id: int, timeout: float = 3600, poll: float = 0.5) -> Job:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = get(job_id)
        if job.status in ("completed", "failed", "cancelled"):
            return job
        time.sleep(poll)
    raise TimeoutError(f"job {job_id} still {get(job_id).status} after {timeout}s")


def as_dict(job: Job, with_logs: bool = False) -> dict[str, Any]:
    d = {"id": job.id, "kind": job.kind, "status": job.status, "progress": round(job.progress, 3),
         "message": job.message, "payload": job.payload, "result": job.result, "error": job.error,
         "attempts": job.attempts, "max_attempts": job.max_attempts, "cancel_requested": job.cancel_requested,
         "created_at": _iso(job.created_at), "started_at": _iso(job.started_at),
         "finished_at": _iso(job.finished_at)}
    if with_logs:
        d["logs"] = [{"at": _iso(x.at), "level": x.level, "message": x.message} for x in logs(job.id)]
    return d


def _iso(dt: datetime | None) -> str | None:
    dt = db.aware(dt)
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None
