"""Background work for the MCP server: transcription and rendering take
minutes, longer than an agent should block on a single tool call."""

from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import db

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clipper-job")
_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def start(kind: str, fn: Callable[[], Any], **meta: Any) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:8]
    job = {"job_id": job_id, "kind": kind, "status": "running", "started_at": db.now(), **meta}
    with _lock:
        _jobs[job_id] = job

    def run() -> None:
        try:
            result = fn()
            job.update(status="done", result=result, finished_at=db.now())
        except Exception as e:  # surfaced through job_status
            job.update(status="failed", error=f"{type(e).__name__}: {e}",
                       trace=traceback.format_exc()[-1500:], finished_at=db.now())

    _pool.submit(run)
    return {k: v for k, v in job.items() if k != "result"}


def status(job_id: str | None = None) -> Any:
    with _lock:
        if job_id is None:
            return [{k: v for k, v in j.items() if k not in ("result", "trace")} for j in _jobs.values()]
        if job_id not in _jobs:
            raise LookupError(f"no job {job_id} (jobs live only as long as the MCP server process)")
        return dict(_jobs[job_id])
