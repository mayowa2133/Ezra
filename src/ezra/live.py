"""Live clipping: stream → rolling segments → transcript windows → candidate
moments → review queue.

Sources
  file     a local recording replayed at `speed`x real time (testing, demos)
  hls      any HLS/RTMP/HTTP stream URL ffmpeg can read
  youtube  a YouTube Live URL resolved to its HLS manifest with yt-dlp; only for
           streams you own or are authorized to clip
Twitch/Kick are deliberately not bundled: their terms restrict automated
capture and reuse of other people's streams.

Each window (the last `window_seconds` of the stream) is ingested as a Source,
analyzed and scouted; candidates starting in material not seen by the previous
window are kept (origin "live") and, optionally, rendered for review.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import candidates, db, render, sources
from .config import get_settings
from .db.models import Candidate


class LiveSource(ABC):
    name: str

    @abstractmethod
    def input_args(self) -> list[str]: ...


class FileLiveSource(LiveSource):
    name = "file"

    def __init__(self, path: str | Path, speed: float = 1.0):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.speed = speed

    def input_args(self) -> list[str]:
        return ["-readrate", str(self.speed), "-i", str(self.path)]


class HLSLiveSource(LiveSource):
    name = "hls"

    def __init__(self, url: str):
        self.url = url

    def input_args(self) -> list[str]:
        return ["-i", self.url]


class YouTubeLiveSource(LiveSource):
    name = "youtube"

    def __init__(self, url: str):
        if not shutil.which("yt-dlp"):
            raise RuntimeError("YouTube Live needs yt-dlp on PATH")
        out = subprocess.run(["yt-dlp", "-g", "-f", "best[protocol^=m3u8]/best", url], capture_output=True,
                             text=True, timeout=60)
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError(f"could not resolve the live stream: {out.stderr.strip()[-300:]}")
        self.url = out.stdout.strip().splitlines()[0]

    def input_args(self) -> list[str]:
        return ["-i", self.url]


def make_source(kind: str, target: str, speed: float = 1.0) -> LiveSource:
    if kind == "file":
        return FileLiveSource(target, speed)
    if kind == "hls":
        return HLSLiveSource(target)
    if kind == "youtube":
        return YouTubeLiveSource(target)
    raise ValueError("live source kind must be file | hls | youtube")


def run_session(campaign: str | int, live: LiveSource, chunk_seconds: int = 30, window_seconds: int = 120,
                max_seconds: float | None = None, render_top: int = 0, rights_basis: str = "owned",
                progress: Callable[[float, str], None] | None = None,
                should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    say = progress or (lambda f, m: None)
    stop = should_stop or (lambda: False)
    session = f"live-{int(time.time())}"
    work = get_settings().work_dir / session
    work.mkdir(parents=True, exist_ok=True)
    rec = subprocess.Popen(["ffmpeg", "-v", "error", *live.input_args(), *(["-t", str(max_seconds)] if max_seconds
                                                                          else []),
                            "-c", "copy", "-f", "segment", "-segment_time", str(chunk_seconds),
                            "-reset_timestamps", "1", str(work / "seg%05d.ts")])
    processed = 0
    covered_until = 0.0
    found: list[int] = []
    windows = 0
    try:
        while True:
            segs = sorted(work.glob("seg*.ts"))
            finished = rec.poll() is not None
            complete = segs if finished else segs[:-1]   # the newest segment is still being written
            if len(complete) > processed:
                processed = len(complete)
                first = max(0, processed - max(1, window_seconds // chunk_seconds))
                offset = first * chunk_seconds
                win = work / f"window{windows:04d}.mp4"
                lst = work / "list.txt"
                lst.write_text("".join(f"file '{p}'\n" for p in complete[first:processed]))
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                                "-c", "copy", str(win)], check=True)
                say(0.1, f"window {windows}: {offset:.0f}-{processed * chunk_seconds:.0f}s")
                src = sources.ingest(win, campaign=campaign, title=f"{session} {offset:.0f}s",
                                     rights_basis=rights_basis, rights_notes=f"live session {session}")
                cands = candidates.find_candidates(src.id, campaign, max_candidates=10)
                fresh = [c for c in cands if offset + c.start >= covered_until - 1.0]
                for c in fresh:
                    with db.session() as s:
                        row = s.get(Candidate, c.id)
                        if row is not None:
                            row.origin, row.origin_ref = "live", f"{session}:{offset + c.start:.1f}"
                    found.append(c.id)
                covered_until = processed * chunk_seconds
                windows += 1
                if render_top:
                    for c in sorted(fresh, key=lambda c: -(c.rank_score or 0))[:render_top]:
                        if c.compliance_status != "FAIL":
                            render.render_candidate(c.id)
            if finished and len(complete) == processed:
                break
            if stop():
                rec.terminate()
                break
            time.sleep(1.0)
    finally:
        if rec.poll() is None:
            rec.terminate()
    return {"session": session, "windows": windows, "seconds": covered_until, "candidates": found}
