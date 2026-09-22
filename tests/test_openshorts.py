import json
from pathlib import Path

import httpx

from clipper.clipping import clips
from clipper.integrations import openshorts


def mock_openshorts(video_bytes: bytes):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/uploads" and req.method == "POST":
            return httpx.Response(200, json={"upload_id": "u1"})
        if req.url.path == "/api/uploads/u1":
            return httpx.Response(200, json={"upload_id": "u1", "bytes": len(req.content)})
        if req.url.path == "/api/process":
            body = json.loads(req.content)
            assert body["upload_id"] == "u1" and body["acknowledged"] and body["clip_max_seconds"] == 60
            return httpx.Response(200, json={"job_id": "job9"})
        if req.url.path == "/api/status/job9":
            return httpx.Response(200, json={"status": "completed", "logs": ["done"], "result": {"clips": [
                {"start": 0.0, "end": 16.5, "video_title_for_youtube_short": "Lost $400k",
                 "viral_hook_text": "He lost it all 😳", "video_url": "/videos/job9/ep_clip_1.mp4"},
                {"start": 17.0, "end": 22.0, "video_title_for_youtube_short": "Too short",
                 "video_url": "/videos/job9/ep_clip_2.mp4"}]}})
        if req.url.path == "/videos/job9/ep_clip_1.mp4":
            return httpx.Response(200, content=video_bytes)
        return httpx.Response(404)

    client = httpx.Client(base_url="http://os.test", transport=httpx.MockTransport(handler))
    return client, calls


def test_openshorts_roundtrip(campaign, tmp_path):
    client, calls = mock_openshorts(b"fake-mp4")
    job = openshorts.submit(Path(campaign["source"]["path"]), 15, 60, client=client)
    result = openshorts.wait(job, poll=0, client=client)
    imported = openshorts.import_result(campaign["source"]["id"], job, result)
    assert [c["status"] for c in imported] == ["candidate", "rejected_compliance"]
    clip = clips.get(imported[0]["clip_id"])
    assert clip["origin"] == "openshorts" and clip["framing"] == "openshorts" and clip["start_time"] == 0.0
    out = openshorts.download_clip(clip["origin_ref"], tmp_path / "c.mp4", client=client)
    assert out.read_bytes() == b"fake-mp4"
    assert ("PUT", "/api/uploads/u1") in calls
