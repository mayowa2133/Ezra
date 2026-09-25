"""Measure short-form clips the same way, to compare Ezra's renders with a reference set
(e.g. a creator's top-performing Shorts).

    uv run python scripts/compare_shorts.py out.json bench:ref1.mp4 bench:ref2.mp4 ezra:clip1.mp4 ...

Each argument is LABEL:PATH. Per clip: duration, loudness and true peak, scene cuts per second and
median shot, time to first word, words per minute, share of time with speech, longest pause,
letterboxed share (blurred/black bands above and below a sharp middle), share of frames with a
face and the face's size and position, plus the words in the first 2 seconds and the last words.
Compare groups with medians; look at contact sheets for what numbers can't capture (caption style,
graphics, hook cards).
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from statistics import median

import numpy as np

import ezra.transcription  # noqa: F401  (import order)
from ezra.analysis import detect_scenes
from ezra.analysis.faces import get_detector, sample_frames

MODELS = str(Path.home() / ".cache" / "ezra" / "models")
_whisper = None


def whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("small", device="auto", compute_type="int8", download_root=MODELS)
    return _whisper


def probe(p):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                          "-of", "json", str(p)], capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    return float(d["format"]["duration"]), v["width"], v["height"]


def loudness(p):
    e = subprocess.run(["ffmpeg", "-nostats", "-i", str(p), "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", e)
    summ = e[e.rfind("Summary"):]
    pk = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", summ)
    return (float(i[-1]) if i else None), (float(pk[-1]) if pk else None)


def frames_gray(p, fps=2, w=180, h=320):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-vf", f"fps={fps},scale={w}:{h},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    n = len(raw) // (w * h)
    return np.frombuffer(raw[: n * w * h], np.uint8).reshape(n, h, w)


def lap_var(a):
    a = a.astype(np.float32)
    lap = a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    return float(lap.var())


def _edge_line(frame, y):
    """Share of columns with a strong vertical step near row y (the edge of an inset picture)."""
    a = frame.astype(np.int16)
    best = 0.0
    for r in range(max(1, y - 4), min(frame.shape[0] - 1, y + 5)):
        best = max(best, float((np.abs(a[r + 1] - a[r - 1]) > 18).mean()))
    return best


def letterboxed(frame):
    """A 16:9 picture inset in a 9:16 frame over black or blurred padding. Requires the padding to be
    flat AND a full-width edge where the inset picture begins and ends, so dark or plain filled
    frames (night shots, sky, a studio backdrop) with captions in the middle don't count."""
    h, w = frame.shape
    inner = int(w * 9 / 16)
    top_edge, bottom_edge = (h - inner) // 2, (h + inner) // 2
    top, mid, bot = frame[: int(h * .2)], frame[int(h * .4): int(h * .6)], frame[int(h * .8):]
    vt, vm, vb = lap_var(top), lap_var(mid), lap_var(bot)
    flat = (top.mean() < 18 and bot.mean() < 18) or (vm > 40 and vt < 0.12 * vm and vb < 0.12 * vm)
    return bool(flat and _edge_line(frame, top_edge) > 0.5 and _edge_line(frame, bottom_edge) > 0.5)


def analyse(label, path):
    dur, w, h = probe(path)
    lufs, peak = loudness(path)
    scenes = detect_scenes(path)["scenes"]
    cuts = max(0, len(scenes) - 1)
    shot_lens = [s["end"] - s["start"] for s in scenes] or [dur]
    segs, _ = whisper().transcribe(str(path), word_timestamps=True, vad_filter=False, beam_size=1)
    words = [wd for s in segs for wd in (s.words or [])]
    first = words[0].start if words else None
    speech = sum(wd.end - wd.start for wd in words)
    gaps = [b.start - a.end for a, b in zip(words, words[1:])]
    text = " ".join(wd.word.strip() for wd in words)
    first2 = " ".join(wd.word.strip() for wd in words if wd.start < (first or 0) + 2.0)
    fr = frames_gray(path)
    lb = [letterboxed(f) for f in fr]
    faces = sample_frames(path, 0, dur, fps=2.0, detector=get_detector("auto")).faces
    with_face = [fs for _, fs in faces if fs]
    fw = [max(f.w for f in fs) for fs in with_face]
    fcx = [max(fs, key=lambda f: f.w).cx for fs in with_face]
    return {
        "label": label, "file": Path(path).name, "duration": round(dur, 1), "res": f"{w}x{h}",
        "lufs": lufs, "true_peak": peak,
        "cuts": cuts, "cuts_per_sec": round(cuts / dur, 2), "median_shot": round(median(shot_lens), 2),
        "first_word_s": round(first, 2) if first is not None else None,
        "wpm": round(len(words) / max(1e-6, dur) * 60), "speech_share": round(speech / dur, 2),
        "longest_gap": round(max(gaps), 2) if gaps else None,
        "letterboxed_share": round(sum(lb) / max(1, len(lb)), 2),
        "face_share": round(len(with_face) / max(1, len(faces)), 2),
        "face_width_median": round(median(fw), 3) if fw else None,
        "face_cx_median": round(median(fcx), 3) if fcx else None,
        "first_2s": first2, "last_words": " ".join(text.split()[-12:]), "transcript": text,
    }


if __name__ == "__main__":
    out, items = sys.argv[1], sys.argv[2:]
    rows = []
    for it in items:
        label, path = it.split(":", 1)
        try:
            rows.append(analyse(label, path))
            print(label, Path(path).name, "ok", flush=True)
        except Exception as e:
            print(label, path, "FAILED", e, flush=True)
    Path(out).write_text(json.dumps(rows, indent=1))
