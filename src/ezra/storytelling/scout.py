"""Find the usable shots in long highlight packages: every camera shot with its biggest face.

Full-game highlights are ~90% wide court camera; the close-ups (the shots a story is built from)
last 1-3 s each. `shots()` splits a video at camera cuts (PySceneDetect) and measures faces in
each shot with Ezra's detector, so an edit can pick close-ups by size instead of by eye.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class ScoutShot:
    start: float
    end: float
    face_w: float        # biggest face width, fraction of frame width (0 = none): > 0.08 is a close-up
    face_cx: float
    face_cy: float
    faces: int           # most faces seen in one sample

    @property
    def dur(self) -> float:
        return self.end - self.start


def shots(path: Path, min_len: float = 0.6, threshold: float = 27.0) -> list[ScoutShot]:
    from scenedetect import ContentDetector, detect

    from ..analysis.faces import sample_frames

    scenes = detect(str(path), ContentDetector(threshold=threshold))
    out = []
    for a, b in scenes:
        s, e = a.seconds, b.seconds
        if e - s < min_len:
            continue
        # sample the middle of the shot (avoid dissolves at the edges)
        pad = min(0.25, (e - s) * 0.2)
        smp = sample_frames(path, s + pad, e - pad, fps=3, overlays=False)
        best = None
        most = 0
        for _, fs in smp.faces:
            most = max(most, len(fs))
            for f in fs:
                if best is None or f.w > best.w:
                    best = f
        out.append(ScoutShot(round(s, 3), round(e, 3), round(best.w, 3) if best else 0.0,
                             round(best.cx, 3) if best else 0.5, round(best.cy, 3) if best else 0.5, most))
    return out


def closeups(path: Path, min_face: float = 0.08, **kw: Any) -> list[dict[str, Any]]:
    return [asdict(s) | {"dur": round(s.dur, 2)} for s in shots(path, **kw) if s.face_w >= min_face]


def action_audit(shots: list[dict[str, Any]], end: float, base: Path, slack: float = 0.3) -> list[str]:
    """Narrative visual completeness for plays: a shot marked `action` should run until its play
    resolves. Highlight packages cut to a new camera just after the outcome (the make, the catch),
    so the next camera cut in the source marks the end of the play. Returns one warning per shot that
    leaves before it (by more than `slack` seconds of source time)."""
    from scenedetect import ContentDetector, detect

    from .render import Shot, effective_speed

    out = []
    for i, d in enumerate(shots):
        if not d.get("action"):
            continue
        s = Shot(**d)
        dur = (shots[i + 1]["t"] if i + 1 < len(shots) else end) - s.t
        shown_to = s.src_in + dur * effective_speed(s, dur)
        cuts = [a.seconds for a, _ in detect(str(base / s.src), ContentDetector(threshold=27.0),
                                             start_time=max(0.0, s.src_in - 0.1), end_time=s.src_in + 12)]
        nxt = next((c for c in cuts if c > s.src_in + 0.3), None)
        if nxt is not None and shown_to < nxt - slack:
            out.append(f"shot {i} ({s.label}): cuts away at {shown_to:.2f}s, {nxt - shown_to:.2f}s before the play "
                       f"ends ({nxt:.2f}s); extend it, or mark the cut as deliberate suspense")
    return out
