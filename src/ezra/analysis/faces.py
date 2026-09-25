"""Face detection providers and sampling.

haar       OpenCV 4.x bundled Haar cascades (frontal + profile). No download.
           Needs faces >= ~24px at the 960px sampling width.
mediapipe  MediaPipe Tasks BlazeFace short-range (Apache-2.0). More robust to
           pose and lighting; the model (~230 KB) is fetched once from Google's
           official model bucket into $EZRA_HOME/cache/models.
yunet      OpenCV's YuNet CNN detector (cv2.FaceDetectorYN, MIT model from the
           official opencv_zoo, ~227 KB, fetched once). Finds small, turned, dim
           and motion-blurred faces that Haar misses; no extra package.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import get_settings

SAMPLE_WIDTH = 960


@dataclass
class Face:
    cx: float      # centre x as a fraction of frame width
    cy: float
    w: float       # width as a fraction of frame width
    h: float
    score: float = 1.0
    talk: float = 0.0   # mouth movement since the previous sample, net of head movement (active speaker)


Samples = list[tuple[float, list[Face]]]  # (t relative to range start, faces)
Motion = list[tuple[float, float | None, float]]   # (t, motion centre x or None, share of motion near it)
MOTION_WINDOW = 0.32   # crop width of a 9:16 cut from 16:9, as a fraction of source width


class FaceDetector(ABC):
    name: str

    @abstractmethod
    def detect(self, gray: np.ndarray, rgb: np.ndarray | None = None) -> list[Face]: ...


class HaarDetector(FaceDetector):
    name = "haar"

    def __init__(self) -> None:
        import cv2

        self.cv2 = cv2
        root = cv2.data.haarcascades  # type: ignore[attr-defined]
        self.frontal = cv2.CascadeClassifier(root + "haarcascade_frontalface_default.xml")
        self.profile = cv2.CascadeClassifier(root + "haarcascade_profileface.xml")
        self.eye = cv2.CascadeClassifier(root + "haarcascade_eye.xml")

    def _has_eye(self, gray: np.ndarray, box: tuple[int, int, int, int]) -> bool:
        """Frontal Haar fires on textured non-faces (clothing, patterns); a real
        face has an eye in its upper part. Small faces are upscaled first because
        the eye cascade's base window is ~20px."""
        x, y, fw, fh = box
        roi = gray[y:y + int(fh * 0.6), x:x + fw]
        if roi.size == 0:
            return False
        scale = max(1.0, 160 / max(1, fw))
        roi = self.cv2.resize(roi, None, fx=scale, fy=scale)
        return len(self.eye.detectMultiScale(roi, 1.1, 4, minSize=(16, 16))) > 0

    def detect(self, gray: np.ndarray, rgb: np.ndarray | None = None) -> list[Face]:
        h, w = gray.shape
        loose = list(self.frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(24, 24)))
        found = [b for b in loose if self._has_eye(gray, (int(b[0]), int(b[1]), int(b[2]), int(b[3])))]
        if not found and loose:  # eyes can be missed (glasses, tiny faces): demand stronger evidence instead
            found = list(self.frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=10, minSize=(24, 24)))
        if not found:  # speakers turn sideways toward their guest
            found = list(self.profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=8, minSize=(24, 24)))
            if not found:
                flipped = self.cv2.flip(gray, 1)
                found = [(w - x - fw, y, fw, fh) for (x, y, fw, fh)
                         in self.profile.detectMultiScale(flipped, scaleFactor=1.1, minNeighbors=8, minSize=(24, 24))]
        return [Face(float((x + fw / 2) / w), float((y + fh / 2) / h), float(fw / w), float(fh / h))
                for (x, y, fw, fh) in found]


class MediaPipeDetector(FaceDetector):
    name = "mediapipe"
    MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_detector/"
                 "blaze_face_short_range/float16/1/blaze_face_short_range.tflite")

    def __init__(self) -> None:
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision
        except ImportError as e:
            raise RuntimeError("MediaPipe is not installed: `uv sync --extra mediapipe`") from e
        model = get_settings().models_dir / "blaze_face_short_range.tflite"
        if not model.exists():
            model.parent.mkdir(parents=True, exist_ok=True)
            tmp = model.with_suffix(".part")
            urllib.request.urlretrieve(self.MODEL_URL, tmp)
            tmp.replace(model)
        self.mp = mp
        self.detector = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model)), min_detection_confidence=0.5))

    def detect(self, gray: np.ndarray, rgb: np.ndarray | None = None) -> list[Face]:
        if rgb is None:
            rgb = np.stack([gray] * 3, axis=-1)
        h, w = rgb.shape[:2]
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        out = []
        for d in self.detector.detect(image).detections:
            b = d.bounding_box
            out.append(Face((b.origin_x + b.width / 2) / w, (b.origin_y + b.height / 2) / h, b.width / w,
                            b.height / h, float(d.categories[0].score if d.categories else 1.0)))
        return out


class YuNetDetector(FaceDetector):
    name = "yunet"
    MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
                 "face_detection_yunet_2023mar.onnx")
    MODEL_FILE = "face_detection_yunet_2023mar.onnx"
    MODEL_BYTES = 232589
    SCORE = 0.6

    def __init__(self) -> None:
        import cv2

        if not hasattr(cv2, "FaceDetectorYN"):
            raise RuntimeError("this OpenCV build has no FaceDetectorYN (needs OpenCV >= 4.5.4)")
        model = get_settings().models_dir / self.MODEL_FILE
        if not model.exists():
            model.parent.mkdir(parents=True, exist_ok=True)
            tmp = model.with_suffix(".part")
            urllib.request.urlretrieve(self.MODEL_URL, tmp)
            if tmp.stat().st_size != self.MODEL_BYTES:
                tmp.unlink(missing_ok=True)
                raise RuntimeError("YuNet model download was incomplete or changed upstream")
            tmp.replace(model)
        self.cv2 = cv2
        self.net = cv2.FaceDetectorYN.create(str(model), "", (320, 320), self.SCORE, 0.3, 50)
        self.size: tuple[int, int] = (0, 0)

    def detect(self, gray: np.ndarray, rgb: np.ndarray | None = None) -> list[Face]:
        img = (self.cv2.cvtColor(rgb, self.cv2.COLOR_RGB2BGR) if rgb is not None
               else self.cv2.cvtColor(gray, self.cv2.COLOR_GRAY2BGR))
        h, w = img.shape[:2]
        if self.size != (w, h):
            self.net.setInputSize((w, h))
            self.size = (w, h)
        _, found = self.net.detect(img)
        out = []
        for row in (found if found is not None else []):
            x, y, fw, fh, score = float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[-1])
            if fw * fh < 16 * 16:
                continue
            out.append(Face((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h, score))
        return out


def _auto() -> FaceDetector:
    """YuNet when its model is available, else Haar (offline installs keep working)."""
    try:
        return YuNetDetector()
    except Exception:
        return HaarDetector()


DETECTORS: dict[str, Callable[[], FaceDetector]] = {"haar": HaarDetector, "mediapipe": MediaPipeDetector,
                                                    "yunet": YuNetDetector, "auto": _auto}


def get_detector(name: str | None = None) -> FaceDetector:
    name = name or get_settings().face_detector
    if name not in DETECTORS:
        raise ValueError(f"unknown face detector {name!r}; available: {sorted(DETECTORS)}")
    return DETECTORS[name]()


def video_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "json", str(path)], capture_output=True, text=True, check=True)
    s = json.loads(out.stdout)["streams"][0]
    return int(s["width"]), int(s["height"])


def sample_faces(media: Path, start: float, end: float, fps: float = 2.0,
                 detector: FaceDetector | None = None,
                 progress: Callable[[float], None] | None = None) -> Samples:
    """Decode [start, end] at `fps`, scaled to SAMPLE_WIDTH, and detect faces in each frame."""
    return sample_faces_and_motion(media, start, end, fps, detector, progress)[0]


def _patches(gray: np.ndarray, f: Face) -> tuple[np.ndarray, np.ndarray] | None:
    """Mouth (lower face) and eyes (upper face) patches at a fixed size, for talk detection."""
    h, w = gray.shape
    x0, x1 = int((f.cx - f.w * 0.3) * w), int((f.cx + f.w * 0.3) * w)
    mouth_y0, mouth_y1 = int((f.cy + f.h * 0.12) * h), int((f.cy + f.h * 0.45) * h)
    eyes_y0, eyes_y1 = int((f.cy - f.h * 0.35) * h), int((f.cy - f.h * 0.05) * h)
    if x1 - x0 < 8 or mouth_y1 - mouth_y0 < 4 or x0 < 0 or x1 > w or eyes_y0 < 0 or mouth_y1 > h:
        return None

    def norm(a: np.ndarray) -> np.ndarray:
        ys = np.linspace(0, a.shape[0] - 1, 12).astype(int)
        xs = np.linspace(0, a.shape[1] - 1, 24).astype(int)
        return a[np.ix_(ys, xs)].astype(np.float32)

    return norm(gray[mouth_y0:mouth_y1, x0:x1]), norm(gray[eyes_y0:eyes_y1, x0:x1])


def _talk(prev: list[tuple[Face, tuple[np.ndarray, np.ndarray] | None]], f: Face,
          cur: tuple[np.ndarray, np.ndarray] | None) -> float:
    """Mouth change minus eye-region change against the same face in the previous sample."""
    if cur is None or not prev:
        return 0.0
    near = min(prev, key=lambda p: abs(p[0].cx - f.cx) + abs(p[0].cy - f.cy))
    if near[1] is None or abs(near[0].cx - f.cx) > f.w or abs(near[0].cy - f.cy) > f.h:
        return 0.0
    mouth = float(np.abs(cur[0] - near[1][0]).mean())
    eyes = float(np.abs(cur[1] - near[1][1]).mean())
    return round(max(0.0, mouth - eyes) / 255.0, 4)


def _motion(prev: np.ndarray | None, cur: np.ndarray) -> tuple[float | None, float]:
    """Where between two frames things moved: (centre x of the motion, share of all motion
    inside a crop-width window around it). None when the frame barely changed."""
    if prev is None:
        return None, 0.0
    diff = np.abs(cur.astype(np.int16) - prev.astype(np.int16))
    diff[diff < 12] = 0                                   # sensor noise, compression shimmer
    cols = diff.sum(axis=0).astype(np.float64)
    total = float(cols.sum())
    if total / diff.size < 1.5:
        return None, 0.0
    x = np.arange(len(cols)) / max(1, len(cols) - 1)
    cx = float((cols * x).sum() / total)
    half = MOTION_WINDOW / 2
    near = float(cols[(x >= cx - half) & (x <= cx + half)].sum() / total)
    return cx, near


Box = tuple[float, float, float, float]            # x0, y0, x1, y1 as fractions of the frame
Overlays = list[tuple[float, list[Box]]]           # (t, burned-in text / graphics boxes)


STROKE_DENSITY = 1.5   # on/off changes per scanline, per line-height of width
BAND_GAP = 6           # px (at 960 wide) of empty columns that split one band into separate pieces
BAND_INK = 0.5         # share of a tall blob's ink a line band must hold (captions: 0.85+, texture: < 0.3)


def detect_overlays(gray: np.ndarray) -> list[Box]:
    """Burned-in text and graphics (counters, name bars, badges, captions): the classic
    morphological text detector. High-contrast strokes, joined horizontally into line-shaped
    blobs, kept when their size, stroke fill and contrast look like type. Noisy frame by frame
    (lights, railings), so callers keep only boxes that persist across samples."""
    import cv2

    h, w = gray.shape
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    otsu, _ = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    bw = cv2.threshold(grad, max(60.0, otsu), 255, cv2.THRESH_BINARY)[1]
    closed = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, w // 60), 3)))
    n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(closed, connectivity=8)
    out: list[Box] = []
    for i in range(1, n):
        cx, cy, cw, ch, _area = (int(v) for v in stats[i])
        if cw < w * 0.035:
            continue
        for x, y, bw_, bh in _line_bands(closed, cx, cy, cw, ch, h):
            if not (h * 0.018 <= bh <= h * 0.12 and bw_ >= 2.2 * bh and bw_ >= w * 0.035):
                continue
            roi, strokes = gray[y:y + bh, x:x + bw_], bw[y:y + bh, x:x + bw_]
            fill = float((strokes > 0).mean())
            if not (0.18 <= fill <= 0.75 and (float((roi > 200).mean()) > 0.08 or float((roi < 70).mean()) > 0.25)):
                continue
            # type is a row of vertical strokes: many on/off changes along each scanline, about one
            # letter per line-height of width. An edge (a pipe, a railing, a horizon) has almost none.
            mid = strokes[bh // 5: max(bh // 5 + 1, bh - bh // 5)] > 0
            changes = float(np.median(np.count_nonzero(np.diff(mid, axis=1), axis=1)))
            if changes * bh / bw_ >= STROKE_DENSITY:
                out.append((x / w, y / h, (x + bw_) / w, (y + bh) / h))
    return out


def _line_bands(closed: np.ndarray, x: int, y: int, bw: int, bh: int, h: int) -> list[tuple[int, int, int, int]]:
    """A blob as its lines of type. A line that touches scenery (a caption over a door frame)
    joins it into one tall blob; the line's rows stay mostly filled, the scenery's don't."""
    if bh <= h * 0.12:
        return [(x, y, bw, bh)]
    blob = closed[y:y + bh, x:x + bw] > 0
    ink = max(1, int(blob.sum()))
    rows = blob.mean(axis=1) >= 0.5
    bands: list[tuple[int, int, int, int]] = []
    r = 0
    while r < bh:
        if not rows[r]:
            r += 1
            continue
        start = r
        while r < bh and rows[r]:
            r += 1
        # a line's words are already joined, so a gap left in the band separates different things
        # (a roster's name labels under their portraits)
        cols = np.flatnonzero((closed[y + start:y + r, x:x + bw] > 0).mean(axis=0) >= 0.2)
        if cols.size:
            breaks = np.flatnonzero(np.diff(cols) > BAND_GAP) + 1
            for run in np.split(cols, breaks):
                piece = blob[start:r, int(run[0]):int(run[-1]) + 1]
                # the line must be most of the blob: a caption grazing a door frame is; a band of
                # texture (rooftops, grass, a panel's inner rows) is a sliver of a busy blob
                if piece.sum() >= BAND_INK * ink:
                    bands.append((x + int(run[0]), y + start, int(run[-1] - run[0] + 1), r - start))
    return bands


STABLE_RANGE = 14      # grey levels a pixel may drift and still count as not moving
MOVING_RANGE = 32      # ... and must exceed to count as moving
MOVING_SHARE = 0.3     # the scene must be mostly in motion before stillness means "graphic"
MAX_GRAPHICS = 4       # more still regions than this is scenery (a slow push-in), not pasted graphics


def stable_overlays(frames: list[np.ndarray]) -> list[Box]:
    """Graphics pasted over moving footage (a roster panel, a counter, a logo): detailed regions
    that hold perfectly still while most of the picture moves. Says nothing about a locked-off
    shot, where the whole background holds still too."""
    import cv2

    if len(frames) < 5:
        return []           # under ~1.2 s at 4 fps the camera hasn't moved enough to tell
    stack = np.stack(frames).astype(np.int16)
    spread = np.percentile(stack, 90, axis=0) - np.percentile(stack, 10, axis=0)
    if float((spread > MOVING_RANGE).mean()) < MOVING_SHARE:
        return []
    still = np.median(stack, axis=0).astype(np.uint8)
    grad = cv2.morphologyEx(still, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    detail = (grad > 40) & (spread < STABLE_RANGE)
    h, w = still.shape
    joined = cv2.morphologyEx(detail.astype(np.uint8) * 255, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (max(5, w // 40), max(3, h // 40))))
    n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(joined, connectivity=8)
    out: list[Box] = []
    for i in range(1, n):
        x, y, bw_, bh, _area = (int(v) for v in stats[i])
        if bw_ < w * 0.03 or bh < h * 0.02 or bh > 0.3 * h or bw_ * bh > 0.15 * w * h \
                or (bw_ > 0.9 * w and bh < 0.04 * h):
            continue            # specks; scenery that happens to hold still; a source letterbox edge
        if float(detail[y:y + bh, x:x + bw_].mean()) >= 0.08:
            out.append((x / w, y / h, (x + bw_) / w, (y + bh) / h))
    out = [b for b in out if not any(o != b and o[0] <= b[0] and o[1] <= b[1] and b[2] <= o[2] and b[3] <= o[3]
                                     for o in out)]      # a panel's inner detail is the same graphic
    return out if len(out) <= MAX_GRAPHICS else []


@dataclass
class Sampled:
    faces: Samples
    motion: Motion
    overlays: Overlays                                                     # text lines per sample
    thumbs: list[tuple[float, np.ndarray]] = field(default_factory=list)   # small greys for stillness
    stable: dict[float, list[Box]] = field(default_factory=dict)          # still graphics per sample

    def add_stable_overlays(self, bounds: list[tuple[float, float]]) -> None:
        """Per scene, find the graphics that hold still over moving footage."""
        for a, b in bounds:
            boxes = stable_overlays([f for t, f in self.thumbs if a <= t < b])
            for t, _bs in self.overlays:
                if boxes and a <= t < b:
                    self.stable[t] = boxes

    def graphics(self) -> Overlays:
        """Every graphic per sample: text lines plus still panels."""
        return [(t, bs + self.stable.get(t, [])) for t, bs in self.overlays]


def sample_faces_and_motion(media: Path, start: float, end: float, fps: float = 2.0,
                            detector: FaceDetector | None = None,
                            progress: Callable[[float], None] | None = None) -> tuple[Samples, Motion]:
    """Faces per sampled frame, plus a motion track (what moved between samples), from one decode."""
    s = sample_frames(media, start, end, fps, detector, progress)
    return s.faces, s.motion


def sample_frames(media: Path, start: float, end: float, fps: float = 2.0,
                  detector: FaceDetector | None = None, progress: Callable[[float], None] | None = None,
                  overlays: bool = False) -> Sampled:
    """One decode: faces (with talk), motion, and optionally burned-in text/graphics boxes."""
    det = detector or get_detector()
    w, h = video_size(media)
    sh = max(2, int(round(SAMPLE_WIDTH * h / w / 2)) * 2)
    need_rgb = isinstance(det, (MediaPipeDetector, YuNetDetector))
    pix = "rgb24" if need_rgb else "gray"
    frame_bytes = SAMPLE_WIDTH * sh * (3 if need_rgb else 1)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                             "-i", str(media), "-vf", f"fps={fps},scale={SAMPLE_WIDTH}:{sh},format={pix}",
                             "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    out: Samples = []
    motion: Motion = []
    texts: Overlays = []
    thumbs: list[tuple[float, np.ndarray]] = []
    prev_small: np.ndarray | None = None
    prev_faces: list[tuple[Face, tuple[np.ndarray, np.ndarray] | None]] = []
    total = max(1, int((end - start) * fps))
    i = 0
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        arr = np.frombuffer(buf, np.uint8)
        if need_rgb:
            rgb = arr.reshape(sh, SAMPLE_WIDTH, 3)
            gray = rgb.mean(axis=2).astype(np.uint8)
        else:
            rgb, gray = None, arr.reshape(sh, SAMPLE_WIDTH)
        faces = det.detect(gray, rgb)
        now = [(f, _patches(gray, f)) for f in faces]
        for f, patch in now:
            f.talk = _talk(prev_faces, f, patch)
        prev_faces = now
        out.append((i / fps, faces))
        small = gray[::8, ::8]
        mx, share = _motion(prev_small, small)
        motion.append((i / fps, mx, share))
        prev_small = small
        if overlays:
            texts.append((i / fps, detect_overlays(gray)))
            thumbs.append((i / fps, gray[::3, ::3].copy()))
        i += 1
        if progress and i % 20 == 0:
            progress(min(i / total, 1.0))
    proc.wait()
    return Sampled(out, motion, texts, thumbs)
