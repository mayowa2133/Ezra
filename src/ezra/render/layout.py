"""Scene-aware reframing plan.

For each scene of the clip (output timeline) choose a layout:
  track   one subject: crop follows the most prominent face, held steady per shot
  split   two separated faces (vertical/4:5 only): stacked top/bottom crops
  blur    no face (slides, B-roll) or a group: whole frame fitted over a blur
  center  fixed centre crop (static fallback)
The requested layout forces a mode where it is feasible; "auto" decides per scene.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from ..analysis.faces import Face

JUMP = 0.12
CONFIRM = 2
MIN_SHOT = 1.0


@dataclass
class Shot:
    start: float
    end: float
    x: float


@dataclass
class ScenePlan:
    start: float
    end: float
    mode: str
    shots: list[Shot] = field(default_factory=list)       # track: crop centre per shot
    split_x: tuple[float, float] | None = None            # split: (top face x, bottom face x)
    split_y: tuple[float, float] | None = None            # split: face centre heights
    face_w: float | None = None                           # typical face width (fraction of source width)
    faces: int = 0


def plan_shots(samples: list[tuple[float, list[Face]]], start: float, end: float, fps: float) -> list[Shot]:
    """Most prominent face per sample → steady shots that move only on a
    confirmed jump (CONFIRM consecutive samples agreeing on a new spot)."""
    track = [(t, max(fs, key=lambda f: f.w).cx if fs else None) for t, fs in samples if start <= t < end]
    seen = [x for _, x in track if x is not None]
    if not seen:
        return [Shot(start, end, 0.5)]
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
    merged: list[list[tuple[float, float]]] = []
    for s in shots:
        if merged and (s[-1][0] - s[0][0]) + 1 / fps < MIN_SHOT:
            merged[-1].extend(s)
        else:
            merged.append(list(s))
    out = []
    for i, s in enumerate(merged):
        a = start if i == 0 else s[0][0]
        b = merged[i + 1][0][0] if i + 1 < len(merged) else end
        out.append(Shot(round(a, 3), round(b, 3), round(float(median(v for _, v in s)), 4)))
    return out


def _dominant(inside: list[list[Face]], ratio: float = 1.3) -> bool:
    """In most sampled frames, is the biggest face clearly bigger than the next one?"""
    frames = [sorted((f.w for f in fs), reverse=True) for fs in inside if len(fs) >= 2]
    if not frames:
        return False
    return median(1.0 if w[1] <= 0 else w[0] / w[1] for w in frames) >= ratio


MOTION_SHARE = 0.55
SHORT_SHOT = 2.5      # seconds    # a faceless scene is tracked when most of its motion sits in one crop-width band


def _motion_samples(motion: list[tuple[float, float | None, float]] | None, a: float,
                    b: float) -> list[tuple[float, list[Face]]] | None:
    """A faceless scene's subject from its motion track, as pseudo-face samples, or None
    when motion is diffuse (slides, graphics, aerial pans): then the whole frame is shown."""
    if not motion:
        return None
    inside = [(t, x, share) for t, x, share in motion if a <= t < b]
    moving = [(t, x) for t, x, share in inside if x is not None and share >= MOTION_SHARE]
    if not inside or len(moving) < max(2, 0.5 * len(inside)):
        return None
    return [(t, [Face(float(x), 0.5, 0.1, 0.1)]) for t, x in moving]


def plan(samples: list[tuple[float, list[Face]]], duration: float, scene_cuts: list[float], requested: str,
         aspect: str, fps: float, crop_x: float | None = None,
         motion: list[tuple[float, float | None, float]] | None = None) -> list[ScenePlan]:
    bounds = [0.0] + sorted(c for c in scene_cuts if 0.3 < c < duration - 0.3) + [duration]
    vertical = aspect in ("9:16", "4:5")
    plans: list[ScenePlan] = []
    for a, b in zip(bounds, bounds[1:]):
        inside = [fs for t, fs in samples if a <= t < b]
        counts = [len(fs) for fs in inside]
        n = int(median(counts)) if counts else 0
        mode = requested
        pair: tuple[float, float] | None = None
        pair_y: tuple[float, float] | None = None
        face_w = float(median(max(f.w for f in fs) for fs in inside if fs)) if any(inside) else None
        if n >= 2:
            twos = [sorted(fs, key=lambda f: f.cx) for fs in inside if len(fs) >= 2]
            left = median(f[0].cx for f in twos)
            right = median(f[-1].cx for f in twos)
            if right - left >= 0.25:
                pair = (float(left), float(right))
                pair_y = (float(median(f[0].cy for f in twos)), float(median(f[-1].cy for f in twos)))
        moving = None
        if requested == "auto" and n == 0 and vertical:
            moving = _motion_samples(motion, a, b)
        if crop_x is not None and requested in ("auto", "track", "center", "static"):
            mode = "static"
        elif requested == "auto":
            if n == 0:
                # fill the frame with the action when it's clear where it is; a short faceless shot
                # (fast-cut edits compose on-centre) fills from the centre, so the clip doesn't flip
                # between full-frame and letterboxed every second; long ones (graphics, text,
                # establishing aerials) are shown whole
                mode = "track" if moving else ("center" if vertical and b - a < SHORT_SHOT else "blur")
            elif n == 2 and pair and vertical:
                mode = "split"
            elif n >= 3 and not _dominant(inside):
                mode = "blur"      # a panel of equals: without knowing who talks, show everyone
            else:
                # one face, or a group with someone nearest the camera: follow them, filling the
                # frame the way strong vertical clips do
                mode = "track"
        elif requested == "split" and not (pair and vertical):
            mode = "track"
        sp = ScenePlan(a, b, mode, faces=n, split_x=pair, split_y=pair_y, face_w=face_w)
        if mode == "track":
            sp.shots = plan_shots(moving or samples, a, b, fps)
        elif mode in ("static", "center"):
            sp.shots = [Shot(a, b, crop_x if crop_x is not None else 0.5)]
        plans.append(sp)
    return plans


def x_expr(pieces: list[tuple[float, float, float]], axis: str = "x") -> str:
    """ffmpeg crop offset expression switching centre per (start, end, centre)
    piece, clamped inside the frame (`t` relative to the clip start).
    axis "x" uses input/output width, "y" height."""
    i, o = ("iw", "ow") if axis == "x" else ("ih", "oh")

    def at(c: float) -> str:
        return f"max(0,min({i}-{o},{c:.4f}*{i}-{o}/2))"

    if not pieces:
        return at(0.5)
    expr = at(pieces[-1][2])
    for _, b, c in reversed(pieces[:-1]):
        expr = f"if(lt(t,{b:.3f}),{at(c)},{expr})"
    return expr


def enable_expr(ranges: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{a:.3f},{b - 0.001:.3f})" for a, b in ranges) or "0"


def summary(plans: list[ScenePlan]) -> str:
    modes = sorted({p.mode for p in plans})
    return modes[0] if len(modes) == 1 else "mixed:" + ",".join(modes)
