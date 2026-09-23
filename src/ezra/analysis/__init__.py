"""Source analysis: transcript, speakers, scenes, faces, silence and text
signals, each stored as a SourceAnalysis row with provider, version and
confidence. `analyze_source` is resumable: finished products are skipped."""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import select

from .. import costs, db, sources
from ..db.models import SourceAnalysis

Progress = Callable[[float, str], None]
VERSION = "1"


def store(source_id: int, kind: str, provider: str, version: str, confidence: float | None,
          data: dict[str, Any]) -> SourceAnalysis:
    with db.session() as s:
        row = s.scalar(select(SourceAnalysis).where(SourceAnalysis.source_id == source_id,
                                                    SourceAnalysis.kind == kind, SourceAnalysis.version == version))
        if row is None:
            row = SourceAnalysis(source_id=source_id, kind=kind, provider=provider, version=version)
            s.add(row)
        row.provider = provider
        row.confidence = confidence
        row.data = data
        s.flush()
        return row


def get(source_id: int, kind: str) -> SourceAnalysis | None:
    with db.session() as s:
        return s.scalar(select(SourceAnalysis).where(SourceAnalysis.source_id == source_id,
                                                     SourceAnalysis.kind == kind)
                        .order_by(SourceAnalysis.id.desc()).limit(1))


def all_for(source_id: int) -> dict[str, SourceAnalysis]:
    with db.session() as s:
        rows = s.scalars(select(SourceAnalysis).where(SourceAnalysis.source_id == source_id)
                         .order_by(SourceAnalysis.id))
        return {r.kind: r for r in rows}


# --- individual analyzers ------------------------------------------------------

def detect_scenes(media: Path) -> dict[str, Any]:
    import logging

    from scenedetect import ContentDetector, SceneManager, StatsManager, open_video

    logging.getLogger("pyscenedetect").setLevel(logging.ERROR)   # it (re)configures its own INFO logger on import
    video = open_video(str(media))
    stats = StatsManager()
    sm = SceneManager(stats_manager=stats)
    sm.add_detector(ContentDetector(threshold=27.0))
    sm.auto_downscale = True
    sm.detect_scenes(video, show_progress=False)
    scenes = [{"start": round(a.seconds, 3), "end": round(b.seconds, 3)} for a, b in sm.get_scene_list()]
    fps = video.frame_rate or 25.0
    # visual activity: mean content change per second
    activity: list[float] = []
    try:
        vals = []
        for frame in range(0, video.duration.frame_num if video.duration else 0):
            m = stats.get_metrics(frame, ["content_val"])
            vals.append(m[0] if m and m[0] is not None else 0.0)
        per = int(round(fps))
        activity = [round(sum(vals[i:i + per]) / max(1, len(vals[i:i + per])), 2) for i in range(0, len(vals), per)]
    except Exception:  # stats are optional; scene cuts are the product
        activity = []
    if not scenes and video.duration:
        scenes = [{"start": 0.0, "end": round(video.duration.seconds, 3)}]
    return {"scenes": scenes, "activity_per_second": activity, "detector": "ContentDetector(27)"}


def detect_silence(media: Path, noise_db: int = -35, min_len: float = 0.5) -> dict[str, Any]:
    out = subprocess.run(["ffmpeg", "-v", "info", "-i", str(media), "-af",
                          f"silencedetect=noise={noise_db}dB:d={min_len}", "-vn", "-f", "null", "-"],
                         capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", out.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out.stderr)]
    regions = [{"start": round(max(0.0, a), 3), "end": round(b, 3)} for a, b in zip(starts, ends)]
    return {"regions": regions, "noise_db": noise_db, "min_len": min_len,
            "total_silence": round(sum(r["end"] - r["start"] for r in regions), 2)}


def detect_loudness(media: Path) -> dict[str, Any]:
    """Per-second loudness (EBU R128 momentary, LUFS, loudest 400 ms block in each second).
    Screams, crashes and crowd noise are where challenge content peaks, and a transcript
    can't see them."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "verbose", "-i", str(media), "-vn", "-af",
                          "ebur128=framelog=verbose", "-f", "null", "-"], capture_output=True, text=True)
    per: dict[int, float] = {}
    for t, m in re.findall(r"t:\s*([\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+|-inf)", out.stderr):
        if m == "-inf":
            continue
        sec = int(float(t))
        per[sec] = max(per.get(sec, -70.0), max(-70.0, float(m)))
    n = max(per) + 1 if per else 0
    series = [round(per.get(i, -70.0), 1) for i in range(n)]
    voiced = sorted(v for v in series if v > -60)
    if not voiced:
        return {"per_second": series, "median": None, "p10": None, "p90": None}
    q = lambda f: voiced[int(f * (len(voiced) - 1))]  # noqa: E731
    return {"per_second": series, "median": q(0.5), "p10": q(0.1), "p90": q(0.9)}


def summarize_faces(samples: list[tuple[float, list[Any]]], scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-scene face statistics and a layout hint (single, two_shot, group, none)."""
    per_scene = []
    for sc in scenes or [{"start": 0.0, "end": samples[-1][0] + 1 if samples else 0.0}]:
        inside = [faces for t, faces in samples if sc["start"] <= t < sc["end"]]
        counts = [len(f) for f in inside]
        n = int(median(counts)) if counts else 0
        xs = sorted(f.cx for faces in inside if len(faces) == n for f in faces) if n else []
        hint = "none" if n == 0 else "single" if n == 1 else "two_shot" if n == 2 else "group"
        if n == 2 and inside:
            gaps = [abs(faces[0].cx - faces[1].cx) for faces in inside if len(faces) == 2]
            if gaps and median(gaps) < 0.25:
                hint = "single"  # two faces close together frame fine as one crop
        per_scene.append({"start": sc["start"], "end": sc["end"], "faces": n, "layout_hint": hint,
                          "x_positions": [round(x, 3) for x in xs[:6]],
                          "detection_rate": round(sum(1 for c in counts if c) / len(counts), 3) if counts else 0.0})
    detected = sum(1 for _, f in samples if f)
    return {"samples": len(samples), "with_faces": detected,
            "detection_rate": round(detected / len(samples), 3) if samples else 0.0, "scenes": per_scene}


# --- orchestration -------------------------------------------------------------

def analyze_source(source_id: int, progress: Progress | None = None, force: bool = False) -> dict[str, Any]:
    from ..transcription import ensure_transcript, load_segments
    from . import faces as face_mod
    from . import text

    say = progress or (lambda f, m: None)
    src = sources.get(source_id)
    media = sources.local_path(src)
    sources.set_status(source_id, "analyzing")
    done = {} if force else all_for(source_id)
    t0 = time.time()
    try:
        say(0.02, "transcribing")
        tr = ensure_transcript(source_id, progress=lambda f: say(0.02 + 0.5 * f, "transcribing"), force=force)
        segments = load_segments(source_id)

        if "scenes" not in done:
            say(0.55, "detecting scenes")
            store(source_id, "scenes", "pyscenedetect", VERSION, None, detect_scenes(media))
        scenes = get(source_id, "scenes").data["scenes"]  # type: ignore[union-attr]

        if "faces" not in done and src.width:
            say(0.65, "detecting faces")
            det = face_mod.get_detector()
            fps = 1.0 if src.duration <= 1800 else 0.5
            samples = face_mod.sample_faces(media, 0, src.duration, fps=fps, detector=det,
                                            progress=lambda f: say(0.65 + 0.2 * f, "detecting faces"))
            summary = summarize_faces(samples, scenes)
            summary["fps"] = fps
            summary["timeline"] = [[round(t, 2), [[round(f.cx, 4), round(f.cy, 4), round(f.w, 4), round(f.h, 4)]
                                                  for f in fs]] for t, fs in samples]
            store(source_id, "faces", det.name, VERSION, summary["detection_rate"], summary)

        if "silence" not in done and src.has_audio:
            say(0.87, "finding silence")
            store(source_id, "silence", "ffmpeg-silencedetect", VERSION, None, detect_silence(media))

        if "loudness" not in done and src.has_audio:
            say(0.9, "measuring loudness")
            store(source_id, "loudness", "ffmpeg-ebur128", VERSION, None, detect_loudness(media))

        say(0.92, "reading the transcript")
        full_text = " ".join(s.text for s in segments)
        topics = text.topic_segments(segments)
        store(source_id, "topics", "texttiling", VERSION, 0.5, {"topics": topics})
        store(source_id, "text_signals", "lexicon", VERSION, 0.4, {
            "speaking_rate": text.speaking_rate(segments),
            "qa_transitions": text.qa_transitions(segments),
            "laughter": text.laughter(segments),
            "emotion_overall": text.emotion_signals(full_text),
            "emotion_per_segment": [text.emotion_signals(s.text)["intensity"] for s in segments],
        })
    except Exception:
        sources.set_status(source_id, "failed")
        raise
    sources.set_status(source_id, "analyzed")
    costs.record("analysis", quantity=time.time() - t0, unit="seconds", source_id=source_id,
                 campaign_id=src.campaign_id)
    say(1.0, "analyzed")
    return summary_for(source_id) | {"transcript_id": tr.id}


def summary_for(source_id: int) -> dict[str, Any]:
    rows = all_for(source_id)
    out: dict[str, Any] = {"source_id": source_id, "products": {}}
    for kind, r in rows.items():
        d = r.data
        brief: dict[str, Any] = {"provider": r.provider, "confidence": r.confidence}
        if kind == "scenes":
            brief["scenes"] = len(d.get("scenes", []))
        elif kind == "faces":
            brief.update(detection_rate=d.get("detection_rate"),
                         layouts=sorted({sc["layout_hint"] for sc in d.get("scenes", [])}))
        elif kind == "speakers":
            brief.update(n_speakers=d.get("n_speakers"), heuristic=d.get("heuristic"))
        elif kind == "loudness":
            brief["median_lufs"] = d.get("median")
        elif kind == "silence":
            brief.update(regions=len(d.get("regions", [])), total_silence=d.get("total_silence"))
        elif kind == "topics":
            brief["topics"] = [t["label"] for t in d.get("topics", [])]
        elif kind == "text_signals":
            brief.update(wpm=d["speaking_rate"]["wpm"], questions=len(d["qa_transitions"]),
                         laughter=len(d["laughter"]))
        out["products"][kind] = brief
    return out
