"""Campaign watermarks: supplied files placed unmodified, for the whole clip.

`remove_white_background` turns a watermark supplied on a flat white background (some campaigns
ship it that way and ask clippers to make it transparent) into a transparent PNG without touching
the mark itself:
  - the background is the near-white area connected to the image border (a flood fill), so white
    lettering enclosed by its glow is never mistaken for background;
  - every other pixel is un-mixed against white: a pixel of value v becomes black at alpha
    (255 - v) / 255, which composites back over white to exactly v. Pure white stays opaque white.
Laid back over white the result reproduces the original (checked, within 1/255 per channel);
the original file is never changed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

WHITE = 254          # at or above this counts as the white background (JPEG-free PNGs are exact)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remove_white_background(src: Path, dst: Path) -> dict[str, Any]:
    """Write a transparent copy of a watermark supplied on white; return what was done and the check."""
    import cv2

    rgb = np.asarray(Image.open(src).convert("RGB")).astype(np.int32)
    if (np.abs(rgb[..., 0] - rgb[..., 1]).max() > 3) or (np.abs(rgb[..., 1] - rgb[..., 2]).max() > 3):
        raise ValueError("watermark isn't neutral grey/white; un-mixing against white would change its colours")
    gray = rgb.mean(axis=2)
    near_white = (gray >= WHITE).astype(np.uint8)
    _n, labels = cv2.connectedComponents(near_white, connectivity=4)
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))) - {0}
    background = np.isin(labels, list(border)) & (near_white == 1)
    ys, xs = np.nonzero(gray < WHITE)
    if len(xs):
        inside = near_white[ys.min():ys.max() + 1, xs.min():xs.max() + 1] == 1
        enclosed = inside & ~background[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        if inside.sum() and enclosed.sum() < 0.5 * inside.sum():
            # white lettering that touches the white background can't be told apart from it:
            # separating them would mean redrawing the letters, which isn't allowed
            raise ValueError("the white lettering touches the white background (no edge between them), so the "
                             "background can't be removed without redrawing the letters; ask for a transparent file")
    alpha = np.where(background, 0, 255).astype(np.float64)
    color = np.full(gray.shape, 255.0)
    mark = ~background & (gray < WHITE)            # glow and anti-aliasing: un-mix against white
    alpha[mark] = 255.0 - gray[mark]
    color[mark] = 0.0
    out = np.dstack([color, color, color, alpha]).round().clip(0, 255).astype(np.uint8)
    Image.fromarray(out, "RGBA").save(dst)
    # proof: the transparent version over white is the original
    a = out[..., 3:4].astype(np.float64) / 255
    over_white = out[..., :3] * a + 255 * (1 - a)
    diff = float(np.abs(over_white - rgb).max())
    enclosed_white = int(((near_white == 1) & ~background).sum())
    return {"source": str(src), "source_sha256": _sha(src), "output": str(dst), "output_sha256": _sha(dst),
            "size": list(out.shape[1::-1]), "transparent_share": round(float(background.mean()), 3),
            "opaque_white_pixels": enclosed_white, "max_diff_over_white": round(diff, 3),
            "matches_original_over_white": diff <= 1.0}


def visible_bbox(path: Path, min_alpha: int = 16) -> tuple[int, int, int, int]:
    """Bounding box (x0, y0, x1, y1) of the visible part of a transparent watermark."""
    a = np.asarray(Image.open(path).convert("RGBA"))[..., 3]
    ys, xs = np.nonzero(a >= min_alpha)
    if not len(xs):
        raise ValueError(f"{path.name} has no visible pixels")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


# --- locked campaign watermark -------------------------------------------------------------------
# Zones on a vertical frame (fractions) that a campaign watermark must stay out of:
CORNER_W, CORNER_H = 0.22, 0.14          # a box this big in each corner
RIGHT_RAIL = (0.80, 0.40, 1.00, 0.92)    # TikTok / Reels / Shorts like, comment, share buttons
BOTTOM_UI = 0.90                         # username and post caption overlay on every platform


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def placement(mark_size: tuple[int, int], bbox: tuple[int, int, int, int], frame: tuple[int, int],
              center: tuple[float, float], visible_width: float,
              avoid: list[tuple[str, tuple[int, int, int, int]]]) -> dict[str, Any]:
    """Where a watermark file goes and at what uniform scale, or a ValueError naming every rule the
    spot breaks. `bbox` is the visible part of the file; `center` places that visible part's centre
    (fractions of the frame); `visible_width` is its width as a share of the frame. `avoid` are the
    rectangles (x0, y0, x1, y1 in pixels) of things it must not touch: captions, the hook card."""
    W, H = frame
    vw, vh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    scale = visible_width * W / vw
    fw, fh = round(mark_size[0] * scale), round(mark_size[1] * scale)
    vis_w, vis_h = vw * scale, vh * scale
    vx0 = center[0] * W - vis_w / 2
    vy0 = center[1] * H - vis_h / 2
    # even pixels: ffmpeg overlays on 4:2:0 video at even positions, so an odd one would be shifted
    x = 2 * round((vx0 - bbox[0] * scale) / 2)
    y = 2 * round((vy0 - bbox[1] * scale) / 2)
    vx0, vy0 = x + bbox[0] * scale, y + bbox[1] * scale
    visible = (vx0, vy0, vx0 + vis_w, vy0 + vis_h)
    problems = []
    if visible[0] < 0 or visible[1] < 0 or visible[2] > W or visible[3] > H:
        problems.append("part of the watermark would be off the frame")
    corners = [(0, 0, CORNER_W * W, CORNER_H * H), ((1 - CORNER_W) * W, 0, W, CORNER_H * H),
               (0, (1 - CORNER_H) * H, CORNER_W * W, H), ((1 - CORNER_W) * W, (1 - CORNER_H) * H, W, H)]
    if any(_overlaps(visible, c) for c in corners):
        problems.append("it sits in a corner")
    rail = (RIGHT_RAIL[0] * W, RIGHT_RAIL[1] * H, RIGHT_RAIL[2] * W, RIGHT_RAIL[3] * H)
    if _overlaps(visible, rail):
        problems.append("it overlaps the platforms' right-side like/comment/share buttons")
    if visible[3] > BOTTOM_UI * H:
        problems.append("it runs into the platforms' bottom caption/username overlay")
    for name, r in avoid:
        if _overlaps(visible, (float(r[0]), float(r[1]), float(r[2]), float(r[3]))):
            problems.append(f"it overlaps the {name}")
    if vis_h < 0.018 * H:
        problems.append(f"too small to read ({vis_h:.0f}px tall)")
    if problems:
        raise ValueError("watermark placement rejected: " + "; ".join(problems))
    return {"x": x, "y": y, "width": fw, "height": fh, "scale": round(scale, 4),
            "visible": [round(v, 1) for v in visible]}
