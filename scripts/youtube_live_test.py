"""Live YouTube check against the real API. Never runs in CI: it needs EZRA_LIVE_YOUTUBE_TEST=1,
real OAuth client credentials and a person at the keyboard for the consent screen.

What it does (everything PRIVATE; nothing is ever made public):
  1. connect a YouTube account (loopback OAuth) unless one is already connected
  2. check the token and permissions (and a live channel lookup)
  3. upload a 5-second generated test pattern as a private video, with a thumbnail
  4. verify the video id and read its metadata back from YouTube
  5. read its statistics (and try YouTube Analytics; usually empty for 1-3 days)
  6. delete it, only if EZRA_LIVE_YOUTUBE_DELETE=1 and the manage permission was granted

    EZRA_LIVE_YOUTUBE_TEST=1 uv run python scripts/youtube_live_test.py
    EZRA_LIVE_YOUTUBE_TEST=1 EZRA_YOUTUBE_MANAGE=1 EZRA_LIVE_YOUTUBE_DELETE=1 uv run python scripts/youtube_live_test.py

Prints a JSON report; exit code 0 only if every required step passed.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def main() -> int:
    if os.environ.get("EZRA_LIVE_YOUTUBE_TEST") != "1":
        print("refusing to run: set EZRA_LIVE_YOUTUBE_TEST=1 (this talks to the real YouTube API)")
        return 2
    from ezra import publishing
    from ezra.config import get_settings
    from ezra.publishing import loopback
    from ezra.publishing.youtube import MANAGE

    report: dict[str, Any] = {"steps": {}}

    def step(name: str, ok: bool, **info: Any) -> None:
        report["steps"][name] = {"ok": ok, **info}
        print(f"[{'ok' if ok else 'FAIL'}] {name} {json.dumps(info, default=str)[:300]}", file=sys.stderr)

    accs = [a for a in publishing.list_accounts("youtube") if a.provider == "youtube" and a.status == "connected"]
    if accs:
        acc = accs[0]
        step("connect", True, account=acc.handle, reused=True)
    else:
        acc = loopback.connect("youtube", get_settings().youtube_loopback_port)
        step("connect", True, account=acc.handle, reused=False)
    health = publishing.account_health(acc.id, check_live=True)
    step("token_and_permissions", bool(health.get("can_upload") and health.get("live_check", {}).get("ok")),
         can_upload=health.get("can_upload"), can_read_analytics=health.get("can_read_analytics"),
         can_manage=health.get("can_manage"), mode=health.get("mode"), live=health.get("live_check"))
    delete = os.environ.get("EZRA_LIVE_YOUTUBE_DELETE") == "1" and MANAGE in (health.get("scopes") or [])
    up = publishing.test_private_upload(acc.id, confirm=True, delete_after=False, actor="live-test")
    step("private_upload", bool(up.get("video_id")), video_id=up.get("video_id"), url=up.get("url"),
         warnings=up.get("warnings"))
    step("verified_on_youtube", bool(up.get("verified_on_youtube")) and up.get("privacy") == "private",
         privacy=up.get("privacy"))
    thumb_warn = [w for w in up.get("warnings") or [] if str(w.get("code", "")).startswith("thumbnail")]
    step("thumbnail", not thumb_warn, detail=thumb_warn or "set", required=False)
    pub = publishing._youtube()
    ctx = publishing._account_ctx(publishing._get_account(acc.id))
    m = pub.metrics(ctx, up["video_id"])
    step("statistics", m is not None, views=getattr(m, "views", None))
    if health.get("can_read_analytics"):
        from datetime import datetime, timedelta

        from ezra.publishing import youtube_analytics

        today = datetime.now(youtube_analytics.PACIFIC).date()
        try:
            rows, _ = youtube_analytics.report(pub, ctx, [up["video_id"]], today - timedelta(days=3), today)
            step("analytics_call", True, rows=len(rows), note="empty is normal for a new video", required=False)
        except Exception as e:  # reported, not fatal: analytics is optional here
            step("analytics_call", False, error=str(e)[:300], required=False)
    if delete:
        pub.delete_video(ctx, up["video_id"])
        step("delete", pub.video(ctx, up["video_id"]) is None, required=False)
    else:
        report["note"] = (f"test video {up.get('url')} left as PRIVATE; delete it in YouTube Studio, or rerun with "
                          "EZRA_YOUTUBE_MANAGE=1 (reconnect) and EZRA_LIVE_YOUTUBE_DELETE=1")
    report["passed"] = all(v["ok"] for v in report["steps"].values() if v.get("required", True))
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
