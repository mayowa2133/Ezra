"""Scene-aware reframing plan.

For each scene of the clip (output timeline) choose a layout:
  track   one subject: crop follows the most prominent face, held steady per shot
  split   two separated faces (vertical/4:5 only): stacked top/bottom crops
  blur    no face (slides, B-roll) or a group: whole frame fitted over a blur
  center  fixed centre crop (static fallback)
The requested layout forces a mode where it is feasible; "auto" decides per scene.

Burned-in graphics (counters, name bars, source captions) are protected: a crop keeps each one
fully in or fully out, and a scene whose large graphic can't be kept whole is shown whole instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from ..analysis.faces import Box, Face, Overlays

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
    graphics: list[Box] = field(default_factory=list)     # persistent burned-in graphics in the scene
    protected: str | None = None                          # "moved" / "shown whole" when graphics decided it


TALK_MIN = 0.012     # mouth movement (net of head movement) that counts as talking
TALK_LEAD = 1.8      # how clearly the talker must out-move everyone else


GROUP_WINDOW = 0.32   # a 9:16 crop of a 16:9 frame, as a fraction of its width


def _crowd(faces: list[Face]) -> Face:
    """The crop-width window holding the most people (weighted by face size), as a pseudo-face."""
    best, score = faces[0], -1.0
    for anchor in faces:
        inside = [f for f in faces if abs(f.cx - anchor.cx) <= GROUP_WINDOW / 2]
        s = sum(f.w for f in inside)
        if s > score:
            cx = sum(f.cx * f.w for f in inside) / s
            cy = sum(f.cy * f.w for f in inside) / s
            best, score = Face(cx, cy, max(f.w for f in inside), max(f.h for f in inside)), s
    return best


def _subject(faces: list[Face]) -> Face:
    """Who to frame: the face that is clearly talking (active speaker), else a face clearly nearest
    the camera, else (in a group) the window with the most people."""
    biggest = max(faces, key=lambda f: f.w)
    if len(faces) < 2:
        return biggest
    ranked = sorted(faces, key=lambda f: f.talk, reverse=True)
    if ranked[0].talk >= TALK_MIN and ranked[0].talk >= TALK_LEAD * max(ranked[1].talk, 1e-4):
        return ranked[0]
    widths = sorted((f.w for f in faces), reverse=True)
    if len(faces) >= 3 and widths[0] < 1.3 * widths[1]:
        return _crowd(faces)
    return biggest


def plan_shots(samples: list[tuple[float, list[Face]]], start: float, end: float, fps: float) -> list[Shot]:
    """Most prominent face per sample → steady shots that move only on a
    confirmed jump (CONFIRM consecutive samples agreeing on a new spot)."""
    track = [(t, _subject(fs).cx if fs else None) for t, fs in samples if start <= t < end]
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


MOTION_SHARE = 0.55   # a faceless scene is tracked when most of its motion sits in one crop-width band
SHORT_SHOT = 2.5      # seconds: a static shot at least this long (graphic, slide) is shown whole


def _is_static(motion: list[tuple[float, float | None, float]] | None, a: float, b: float) -> bool:
    """Barely anything moves: a graphic, a text slide, a held establishing shot."""
    inside = [x for t, x, _ in (motion or []) if a <= t < b]
    return bool(inside) and sum(x is None for x in inside) >= 0.7 * len(inside)


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


def scene_bounds(duration: float, scene_cuts: list[float]) -> list[tuple[float, float]]:
    bounds = [0.0] + sorted(c for c in scene_cuts if 0.3 < c < duration - 0.3) + [duration]
    return list(zip(bounds, bounds[1:]))


OVERLAY_PERSIST = 0.5   # share of a scene's samples a graphic must appear in (detections are noisy)
OVERLAY_IOU = 0.4
ROW_GAP = 0.06          # pieces this close on the same line are one graphic
BIG_OVERLAY = 0.12      # a graphic this wide (share of source width) is worth showing the shot whole
SUBJECT_SLACK = 0.6     # how far (share of the crop's half-width) the crop may slide off its subject
EDGE_MARGIN = 0.006


def _iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def persistent_overlays(overlays: Overlays | None, a: float, b: float) -> list[Box]:
    """Graphics seen in at least half the samples of [a, b): the per-frame detector also fires on
    lights and railings for a frame or two; burned-in graphics hold still."""
    frames = [bs for t, bs in overlays or [] if a <= t < b]
    if len(frames) < 2:
        return []
    clusters: list[list[Box]] = []
    for bs in frames:
        for box in bs:
            for c in clusters:
                if _iou(c[0], box) > OVERLAY_IOU:
                    c.append(box)
                    break
            else:
                clusters.append([box])
    need = max(2.0, OVERLAY_PERSIST * len(frames))
    return _rows([(float(median(x[0] for x in c)), float(median(x[1] for x in c)),
                   float(median(x[2] for x in c)), float(median(x[3] for x in c)))
                  for c in clusters if len(c) >= need])


def _rows(boxes: list[Box]) -> list[Box]:
    """Join pieces of one graphic that sit side by side (a roster's name labels, a split ticker)."""
    merged = sorted(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i], merged[j]
                ha, hb = a[3] - a[1], b[3] - b[1]
                overlap = min(a[3], b[3]) - max(a[1], b[1])
                gap = max(a[0], b[0]) - min(a[2], b[2])
                # one line of pieces of similar height; don't chain scenery into a giant box
                if overlap >= 0.5 * min(ha, hb) and max(ha, hb) <= 2 * min(ha, hb) and gap <= ROW_GAP:
                    merged[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    del merged[j]
                    changed = True
                    break
            if changed:
                break
    return merged


GRAPHIC_RUN = 3        # samples a large graphic must stay (or stay gone) to split a scene around it
CUT_SNAP = 0.5         # a graphic appearing this close to a scene cut arrived with the cut


def graphic_changes(overlays: Overlays, fps: float, scene_cuts: list[float], duration: float) -> list[float]:
    """Times inside a scene where a large graphic comes or goes (a caption that runs across a
    cut, a counter popping up), so the part with it and the part without are planned apart."""
    present = [(t, any(b[2] - b[0] >= BIG_OVERLAY for b in bs)) for t, bs in overlays]
    runs: list[list[Any]] = []            # [first t, last t, present, samples]
    for t, on in present:
        if runs and runs[-1][2] == on:
            runs[-1][1], runs[-1][3] = t, runs[-1][3] + 1
        else:
            runs.append([t, t, on, 1])
    step = 1 / fps
    out: list[float] = []
    for prev, run in zip(runs, runs[1:]):
        if prev[3] < GRAPHIC_RUN or run[3] < GRAPHIC_RUN:
            continue
        c = round(run[0] - step / 2, 3)
        if 0.3 < c < duration - 0.3 and all(abs(c - x) > CUT_SNAP for x in scene_cuts + out):
            out.append(c)
    return out


def _cut(c: float, half: float, boxes: list[Box]) -> list[Box]:
    """Boxes a crop centred on `c` would slice through."""
    lo, hi = c - half, c + half
    return [b for b in boxes if b[0] + EDGE_MARGIN < lo < b[2] - EDGE_MARGIN
            or b[0] + EDGE_MARGIN < hi < b[2] - EDGE_MARGIN]


def protect_x(x: float, boxes: list[Box], half: float, slack: float) -> tuple[float, bool]:
    """The crop centre nearest `x` (within `slack`) that keeps every graphic fully in or fully out.
    Returns (centre, True), or (x, False) when no such centre exists."""
    clamp = (lambda c: min(max(c, half), 1 - half)) if half < 0.5 else (lambda c: 0.5)
    x = clamp(x)
    if not _cut(x, half, boxes):
        return x, True
    options = {clamp(x - slack), clamp(x + slack)}
    for b in boxes:
        m = 2 * EDGE_MARGIN
        options |= {clamp(b[2] - half + m), clamp(b[0] + half - m),      # fully in
                    clamp(b[2] + half + m), clamp(b[0] - half - m)}      # fully out
    ok = [c for c in options if abs(c - x) <= slack + 1e-9 and not _cut(c, half, boxes)]
    return (min(ok, key=lambda c: abs(c - x)), True) if ok else (x, False)


def _protect(sp: ScenePlan, overlays: Overlays | None, half: float, vertical: bool, auto: bool) -> None:
    """Slide the scene's crops so graphics stay whole; show the scene whole when a large graphic
    can't be kept whole any other way (auto layout on vertical output only)."""
    sp.graphics = persistent_overlays(overlays, sp.start, sp.end)
    if not sp.graphics or sp.mode not in ("track", "center"):
        return
    slack = SUBJECT_SLACK * half if sp.mode == "track" else half
    moved, stuck = False, False
    for shot in sp.shots:
        boxes = persistent_overlays(overlays, shot.start, shot.end) or sp.graphics
        x, ok = protect_x(shot.x, boxes, half, slack)
        if ok and abs(x - min(max(shot.x, half), 1 - half)) > 1e-6:
            shot.x, moved = x, True
        if not ok and any(b[2] - b[0] >= BIG_OVERLAY for b in _cut(shot.x, half, boxes)):
            stuck = True
    if stuck and auto and vertical:
        sp.mode, sp.shots, sp.protected = "blur", [], "shown whole"
    elif moved:
        sp.protected = "moved"


def plan(samples: list[tuple[float, list[Face]]], duration: float, scene_cuts: list[float], requested: str,
         aspect: str, fps: float, crop_x: float | None = None,
         motion: list[tuple[float, float | None, float]] | None = None,
         overlays: Overlays | None = None, crop_frac: float | None = None) -> list[ScenePlan]:
    """`overlays` (burned-in graphics per sample) and `crop_frac` (crop width as a share of the
    source width) turn on graphics protection."""
    vertical = aspect in ("9:16", "4:5")
    plans: list[ScenePlan] = []
    if overlays and crop_frac and crop_frac < 1:
        scene_cuts = scene_cuts + graphic_changes(overlays, fps, scene_cuts, duration)
    for a, b in scene_bounds(duration, scene_cuts):
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
                # Top-performing vertical clips are never letterboxed (0% across 30 MrBeast Shorts
                # with 100M+ views): follow the action when it's clear where it is, else fill from
                # the centre. Only a long, static shot (a graphic or text slide) is shown whole.
                if moving:
                    mode = "track"
                elif vertical and not (b - a >= SHORT_SHOT and _is_static(motion, a, b)):
                    mode = "center"
                else:
                    mode = "blur"
            elif n == 2 and pair and vertical:
                mode = "split"
            elif n >= 3 and not _dominant(inside) and not vertical:
                mode = "blur"      # landscape output of a panel: show everyone
            else:
                # one face or a group: follow the active speaker, the face nearest the camera, or
                # the densest cluster of people; vertical output always fills the frame
                mode = "track"
        elif requested == "split" and not (pair and vertical):
            mode = "track"
        sp = ScenePlan(a, b, mode, faces=n, split_x=pair, split_y=pair_y, face_w=face_w)
        if mode == "track":
            sp.shots = plan_shots(moving or samples, a, b, fps)
        elif mode in ("static", "center"):
            sp.shots = [Shot(a, b, crop_x if crop_x is not None else 0.5)]
        if overlays and crop_frac and crop_frac < 1 and mode != "static":
            _protect(sp, overlays, crop_frac / 2, vertical, requested == "auto")
        plans.append(sp)
    return plans


SOURCE_CAPTION_W = 0.25    # a text line this wide (share of source width) low in the frame is a caption
SOURCE_CAPTION_Y = 0.55    # ... with its centre below this height
SOURCE_CAPTION_MIN = 2     # consecutive samples, so a flicker of texture doesn't count
SOURCE_CAPTION_GAP = 0.5   # seconds between two runs that still count as one caption
SOURCE_CAPTION_NEIGHBOUR = 0.08   # another piece this close on the same row makes it a row of labels


def _visible_share(box: Box, sp: ScenePlan, t: float, half: float) -> float:
    """How much of a source box's width the output shows at time t."""
    if sp.mode == "blur":
        return 1.0
    if sp.mode not in ("track", "center", "static") or not sp.shots:
        return 0.0
    shot = next((s for s in sp.shots if s.start <= t < s.end), sp.shots[-1])
    c = min(max(shot.x, half), 1 - half) if half < 0.5 else 0.5
    inside = max(0.0, min(box[2], c + half) - max(box[0], c - half))
    return inside / max(1e-6, box[2] - box[0])


def _in_panel(line: Box, panels: list[Box]) -> bool:
    """A line of type inside a still panel much taller than itself: a graphic's label (a roster's
    names), not a caption."""
    lh = line[3] - line[1]
    return any(p[0] - 0.01 <= line[0] and line[2] <= p[2] + 0.01 and p[1] - 0.01 <= line[1]
               and line[3] <= p[3] + 0.01 and p[3] - p[1] >= 2 * lh for p in panels)


def _alone(line: Box, boxes: list[Box]) -> bool:
    """No other piece of type beside it on the same row. A roster or scoreboard is a row of
    separate labels; a caption line stands alone."""
    lh = line[3] - line[1]
    for b in boxes:
        overlap = min(line[3], b[3]) - max(line[1], b[1])
        gap = max(line[0], b[0]) - min(line[2], b[2])
        if 0 < gap <= SOURCE_CAPTION_NEIGHBOUR and overlap >= 0.5 * min(lh, b[3] - b[1]):
            return False
    return True


def source_caption_spans(plans: list[ScenePlan], overlays: Overlays | None, crop_frac: float | None,
                         fps: float, panels: dict[float, list[Box]] | None = None) -> list[tuple[float, float]]:
    """When the output shows the source's own burned-in captions: a wide line of type low in the
    frame, on screen for a few samples, not part of a larger panel. Ezra's captions step aside
    there instead of stacking a second set of words over the first."""
    if not overlays or not plans:
        return []
    half = (crop_frac or 1.0) / 2
    hits: list[float] = []
    for t, boxes in overlays:
        sp = next((p for p in plans if p.start <= t < p.end), plans[-1])
        around = (panels or {}).get(t, [])
        # one detected line (the detector already joins a caption's words); separate labels in a
        # row, like a roster's names, aren't a caption
        for b in boxes:
            if (b[2] - b[0] >= SOURCE_CAPTION_W and b[3] - b[1] <= 0.12 and (b[1] + b[3]) / 2 >= SOURCE_CAPTION_Y
                    and _alone(b, boxes) and not _in_panel(b, around) and _visible_share(b, sp, t, half) >= 0.5):
                hits.append(t)
                break
    runs: list[tuple[float, float, int]] = []
    step = 1 / fps
    for t in hits:
        if runs and t - runs[-1][1] <= step * 1.5:
            runs[-1] = (runs[-1][0], t, runs[-1][2] + 1)
        else:
            runs.append((t, t, 1))
    spans: list[tuple[float, float]] = []
    for first, last, n in runs:
        if n < SOURCE_CAPTION_MIN:
            continue
        lo, hi = round(max(0.0, first - step / 2), 3), round(last + step, 3)
        if spans and lo - spans[-1][1] <= SOURCE_CAPTION_GAP:
            spans[-1] = (spans[-1][0], hi)         # a missed detection doesn't flash ours back on
        else:
            spans.append((lo, hi))
    return spans


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
