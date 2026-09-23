"""End-to-end smoke test against a running Ezra API (e.g. the docker-compose stack).

    uv run python scripts/smoke_api.py --api http://localhost:8000 --token $EZRA_API_TOKEN \
        --video ~/.cache/ezra/fixtures/podcast.mp4

Campaign import → upload (rights: campaign_supplied) → analyze + candidates →
render top 2 → review card + ranged media → approve → local-export publish
(dry run, then confirmed) → metrics → earnings. It creates real rows; point it
at a disposable deployment. Exits non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--token", default=None)
    ap.add_argument("--video", required=True, type=Path)
    ap.add_argument("--campaign-file", type=Path, default=ROOT / "campaigns" / "demo-campaign.yaml")
    ap.add_argument("--timeout", type=float, default=1200)
    a = ap.parse_args()

    h = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    c = httpx.Client(base_url=a.api, headers=h, timeout=120)
    t0 = time.time()

    def step(msg: str) -> None:
        print(f"[{time.time() - t0:6.1f}s] {msg}", flush=True)

    def ok(r: httpx.Response) -> dict:
        if r.status_code >= 400:
            raise SystemExit(f"FAIL {r.request.method} {r.request.url}: {r.status_code} {r.text[:400]}")
        return r.json()

    def wait(job: dict) -> dict:
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            j = ok(c.get(f"/api/jobs/{job['id']}"))
            if j["status"] in ("completed", "failed", "cancelled"):
                if j["status"] != "completed":
                    raise SystemExit(f"FAIL job {j['id']} {j['kind']}: {j['status']} {j.get('error')}")
                return j
            time.sleep(2)
        raise SystemExit(f"FAIL job {job['id']} timed out")

    ok(c.get("/api/health"))
    step("health ok")
    camp = ok(c.post("/api/campaigns/import", json={"text": a.campaign_file.read_text()}))[0]
    slug = camp["slug"]
    step(f"campaign {slug}: {len(camp.get('rules', []))} rules")
    with a.video.open("rb") as f:
        src = ok(c.post("/api/sources", files={"file": (a.video.name, f, "video/mp4")},
                        data={"campaign": slug, "rights_basis": "campaign_supplied"}))
    step(f"source {src['id']} uploaded ({src.get('duration')}s, rights {src.get('rights_status')})")
    wait(ok(c.post(f"/api/sources/{src['id']}/analyze")))
    step("analyzed")
    wait(ok(c.post(f"/api/sources/{src['id']}/candidates", json={"campaign": slug})))
    cands = ok(c.get("/api/candidates", params={"campaign": slug, "top": 5}))
    assert cands, "no candidates"
    step(f"{len(cands)} candidates; top {cands[0]['rank_score']}: {cands[0].get('title')!r}")
    wait(ok(c.post("/api/render/top", json={"campaign": slug, "top": 2})))
    queue = ok(c.get("/api/review", params={"campaign": slug}))
    assert queue, "empty review queue"
    card = queue[0]
    step(f"{len(queue)} clips in review; clip {card['clip_id']} layout {card['layout']} "
         f"compliance {card['compliance']['status']}")
    part = httpx.get(card["video_url"], headers={"Range": "bytes=0-1023"}, timeout=60)
    assert part.status_code == 206 and len(part.content) == 1024, part.status_code
    step("signed ranged media ok")
    ok(c.post(f"/api/clips/{card['clip_id']}/approve", json={"reviewer": "smoke"}))
    ok(c.post(f"/api/clips/{card['clip_id']}/metadata", json={"platforms": ["tiktok"]}))
    ok(c.post("/api/accounts", json={"platform": "tiktok", "provider": "local-export", "handle": "smoke"}))
    dry = ok(c.post(f"/api/clips/{card['clip_id']}/publish", json={"platforms": ["tiktok"]}))
    assert dry["dry_run"] and not dry["problems"], dry
    res = ok(c.post(f"/api/clips/{card['clip_id']}/publish", json={"platforms": ["tiktok"], "confirm": True}))
    for jid in res["job_ids"]:
        wait({"id": jid})
    posts = ok(c.get("/api/posts", params={"campaign": slug}))
    assert posts and posts[0]["status"] == "published", posts
    step(f"published post {posts[0]['id']} via local-export")
    ok(c.post(f"/api/posts/{posts[0]['id']}/metrics", json={"views": 12000, "likes": 800}))
    e = ok(c.get(f"/api/campaigns/{slug}/earnings"))
    step(f"earnings: qualified views {e['qualified_views']}, estimated ${e['gross_estimated']}")
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
