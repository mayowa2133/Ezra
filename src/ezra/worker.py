"""Job worker: `python -m ezra.worker` (or `ezra worker`).

Each job runs in a child process by default (EZRA_JOB_ISOLATION):
  * a crashing decoder or model cannot take the worker down,
  * cancellation can stop a long ffmpeg/whisper step immediately,
  * native media libraries that clash when loaded together (OpenCV and PyAV
    both bundle libavdevice on macOS) never share a process.
The parent heartbeats the job while the child runs, so long steps that report
no progress are not mistaken for a dead worker.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time

from . import db, jobs
from .config import get_settings
from .db.models import Job, JobLog, utcnow

log = logging.getLogger("ezra.worker")


def _heartbeat(job_id: int) -> bool:
    """Refresh the heartbeat; returns True when cancellation was requested."""
    with db.session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return True
        job.heartbeat_at = utcnow()
        return bool(job.cancel_requested)


def run_isolated(job: Job) -> Job:
    proc = subprocess.Popen([sys.executable, "-m", "ezra.worker", "--run-job", str(job.id)])
    while True:
        try:
            code = proc.wait(timeout=2)
            break
        except subprocess.TimeoutExpired:
            if _heartbeat(job.id):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                jobs._finish(job.id, "cancelled", message="cancelled while running")
                return jobs.get(job.id)
    after = jobs.get(job.id)
    if after.status == "running":  # child died before recording an outcome
        with db.session() as s:
            s.add(JobLog(job_id=job.id, level="error", message=f"job process exited with code {code}"))
        jobs._record_failure(job.id, RuntimeError(f"job process exited with code {code}"))
    return jobs.get(job.id)


def run_one(job_id: int | None = None, kinds: list[str] | None = None,
            isolation: bool | None = None) -> Job | None:
    job = jobs.claim(job_id, kinds)
    if job is None:
        return None
    iso = get_settings().job_isolation if isolation is None else isolation
    return run_isolated(job) if iso else jobs.run(job)


def run_forever(kinds: list[str] | None = None, once: bool = False) -> None:
    from .publishing import scheduler

    s = get_settings()
    last_maintenance = 0.0
    log.info("worker started (isolation=%s)", s.job_isolation)
    while True:
        if time.time() - last_maintenance > 15:
            requeued = jobs.requeue_stale()
            if requeued:
                log.warning("re-queued stale jobs %s", requeued)
            scheduler.tick()
            last_maintenance = time.time()
        job = run_one(kinds=kinds)
        if job is not None:
            log.info("job %s %s: %s", job.id, job.kind, job.status)
            continue
        if once:
            return
        time.sleep(s.worker_poll_seconds)


NOISY = ("alembic", "httpx", "faster_whisper", "pyscenedetect", "huggingface_hub", "filelock", "urllib3")


def quiet_libraries() -> None:
    """Media/ML libraries log progress and hints at INFO/WARNING through their own
    handlers (PySceneDetect, huggingface_hub's "unauthenticated requests" hint).
    Progress belongs in the job record, so keep them to real errors."""
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.ERROR)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ezra-worker")
    ap.add_argument("--run-job", type=int, help="internal: execute one claimed job in this process")
    ap.add_argument("--kinds", help="comma list of job kinds to take")
    ap.add_argument("--once", action="store_true", help="drain due jobs, then exit")
    args = ap.parse_args(argv)
    if args.run_job:
        # child of a worker or CLI: progress and logs go to the job record, not the terminal
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
        quiet_libraries()
        jobs.run(jobs.get(args.run_job))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    quiet_libraries()
    db.migrate()
    run_forever(kinds=args.kinds.split(",") if args.kinds else None, once=args.once)


if __name__ == "__main__":
    main()
