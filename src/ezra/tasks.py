"""Built-in job tasks. Each task is a thin wrapper around an application
service so the queue, API, CLI and MCP all run the same code."""

from __future__ import annotations

from typing import Any

from .jobs import JobContext, task


@task("noop")
def noop(ctx: JobContext) -> dict[str, Any]:
    """Health check / test task: optionally fails `fail_times` times first."""
    ctx.progress(0.5, "halfway")
    fail_times = int(ctx.payload.get("fail_times", 0))
    marker = ctx.payload.get("marker")
    if marker and fail_times:
        from pathlib import Path

        p = Path(marker)
        n = int(p.read_text()) if p.exists() else 0
        if n < fail_times:
            p.write_text(str(n + 1))
            raise RuntimeError(f"planned failure {n + 1}")
    if ctx.payload.get("sleep"):
        import time

        for _ in range(int(ctx.payload["sleep"] * 10)):
            time.sleep(0.1)
            ctx.check_cancelled()
    return {"ok": True, "echo": ctx.payload.get("echo")}
