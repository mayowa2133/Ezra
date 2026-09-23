"""Speaker-aware 9:16 crop.

Samples the clip twice a second, finds faces, and turns the most prominent
face's position into a few steady *shots*: podcast footage cuts between fixed
camera angles, so a crop that holds still per shot and jumps on the cut looks
edited, where a crop that chases every detection looks like a shaky camera.

Needs OpenCV (the `local` extra); without it, or with no faces found, the crop
falls back to the centre.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from statistics import median
from typing import Callable

SAMPLE_FPS = 2.0
SAMPLE_WIDTH = 960   # Haar needs ~24px faces; at 480 a wide-shot face is ~20px and vanishes
JUMP = 0.12          # move the crop only when the subject shifts this much (fraction of width)
CONFIRM = 2          # ... for this many consecutive samples (ignores one-off misdetections)
MIN_SHOT = 1.0       # seconds

Detections = list[tuple[float, list[tuple[float, float]]]]  # (t, [(center_x, face_width)]) as fractions


@dataclass
class Shot:
    start: float
    end: float
    x: float  # crop centre as a fraction of source width


def video_size(path: str) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", path], capture_output=True, text=True, check=True)
    s = json.loads(out.stdout)["streams"][0]
    return int(s["width"]), int(s["height"])


def _detector() -> Callable | None:
    try:
        import cv2
    except ImportError:
        return None
    frontal = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")

    def detect(gray) -> list[tuple[int, int, int, int]]:
        faces = list(frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(24, 24)))
        if not faces:  # talking heads turn sideways to their guest
            faces = list(profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(24, 24)))
            if not faces:
                flipped = cv2.flip(gray, 1)
                w = gray.shape[1]
                faces = [(w - x - fw, y, fw, fh) for (x, y, fw, fh)
                         in profile.detectMultiScale(flipped, scaleFactor=1.1, minNeighbors=6, minSize=(24, 24))]
        return faces

    return detect


def detect_faces(src: str, start: float, end: float) -> Detections | None:
    """Face positions sampled across [start, end]. None when OpenCV is unavailable."""
    detect = _detector()
    if detect is None:
        return None
    import numpy as np

    w, h = video_size(src)
    sh = max(2, int(round(SAMPLE_WIDTH * h / w / 2)) * 2)
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", src,
         "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_WIDTH}:{sh},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True)
    frame_bytes = SAMPLE_WIDTH * sh
    out: Detections = []
    for i in range(len(proc.stdout) // frame_bytes):
        gray = np.frombuffer(proc.stdout, np.uint8, frame_bytes, i * frame_bytes).reshape(sh, SAMPLE_WIDTH)
        faces = detect(gray)
        out.append((i / SAMPLE_FPS, [(float((x + fw / 2) / SAMPLE_WIDTH), float(fw / SAMPLE_WIDTH))
                                     for (x, y, fw, fh) in faces]))
    return out


def plan_shots(samples: Detections, duration: float) -> list[Shot]:
    """Most prominent face per sample → steady shots."""
    track: list[tuple[float, float | None]] = []
    for t, faces in samples:
        track.append((t, max(faces, key=lambda f: f[1])[0] if faces else None))
    seen = [x for _, x in track if x is not None]
    if not seen:
        return [Shot(0.0, duration, 0.5)]
    # Fill gaps (no face this sample) with the last known position, leading gaps with the first.
    last = seen[0]
    filled = []
    for t, x in track:
        last = x if x is not None else last
        filled.append((t, last))

    shots: list[list[tuple[float, float]]] = [[filled[0]]]
    pending: list[tuple[float, float]] = []
    for t, x in filled[1:]:
        centre = median(v for _, v in shots[-1])
        if abs(x - centre) > JUMP:
            # A jump counts once CONFIRM consecutive samples agree on the new spot;
            # earlier outliers that disagree are treated as noise in the current shot.
            shots[-1].extend(p for p in pending if abs(p[1] - x) > JUMP)
            pending = [p for p in pending if abs(p[1] - x) <= JUMP] + [(t, x)]
            if len(pending) >= CONFIRM:
                shots.append(pending)
                pending = []
            continue
        shots[-1].extend(pending + [(t, x)])
        pending = []
    if pending:
        shots[-1].extend(pending)

    # Merge shots too short to read as a cut into their predecessor.
    merged: list[list[tuple[float, float]]] = []
    for s in shots:
        span = (s[-1][0] - s[0][0]) + 1 / SAMPLE_FPS
        if merged and span < MIN_SHOT:
            merged[-1].extend(s)
        else:
            merged.append(list(s))
    out = []
    for i, s in enumerate(merged):
        start = 0.0 if i == 0 else s[0][0]
        end = merged[i + 1][0][0] if i + 1 < len(merged) else duration
        out.append(Shot(round(start, 3), round(end, 3), round(median(v for _, v in s), 4)))
    return out


def crop_x_expr(shots: list[Shot]) -> str:
    """ffmpeg crop `x` expression: each shot's centre, clamped inside the frame,
    switching at shot boundaries (`t` is relative to the clip start)."""
    def at(c: float) -> str:
        return f"max(0,min(iw-ow,{c:.4f}*iw-ow/2))"

    expr = at(shots[-1].x)
    for s in reversed(shots[:-1]):
        expr = f"if(lt(t,{s.end:.3f}),{at(s.x)},{expr})"
    return expr


def auto_shots(src: str, start: float, end: float) -> list[Shot] | None:
    samples = detect_faces(src, start, end)
    if samples is None:
        return None
    return plan_shots(samples, end - start)
