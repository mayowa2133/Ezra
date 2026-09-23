"""Face detection providers and sampling.

haar       OpenCV 4.x bundled Haar cascades (frontal + profile). No download.
           Needs faces >= ~24px at the 960px sampling width.
mediapipe  MediaPipe Tasks BlazeFace short-range (Apache-2.0). More robust to
           pose and lighting; the model (~230 KB) is fetched once from Google's
           official model bucket into $EZRA_HOME/cache/models.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
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


DETECTORS: dict[str, Callable[[], FaceDetector]] = {"haar": HaarDetector, "mediapipe": MediaPipeDetector}


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


def sample_faces_and_motion(media: Path, start: float, end: float, fps: float = 2.0,
                            detector: FaceDetector | None = None,
                            progress: Callable[[float], None] | None = None) -> tuple[Samples, Motion]:
    """Faces per sampled frame, plus a motion track (what moved between samples), from one decode."""
    det = detector or get_detector()
    w, h = video_size(media)
    sh = max(2, int(round(SAMPLE_WIDTH * h / w / 2)) * 2)
    need_rgb = isinstance(det, MediaPipeDetector)
    pix = "rgb24" if need_rgb else "gray"
    frame_bytes = SAMPLE_WIDTH * sh * (3 if need_rgb else 1)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                             "-i", str(media), "-vf", f"fps={fps},scale={SAMPLE_WIDTH}:{sh},format={pix}",
                             "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    out: Samples = []
    motion: Motion = []
    prev_small: np.ndarray | None = None
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
        out.append((i / fps, det.detect(gray, rgb)))
        small = gray[::8, ::8]
        mx, share = _motion(prev_small, small)
        motion.append((i / fps, mx, share))
        prev_small = small
        i += 1
        if progress and i % 20 == 0:
            progress(min(i / total, 1.0))
    proc.wait()
    return out, motion
