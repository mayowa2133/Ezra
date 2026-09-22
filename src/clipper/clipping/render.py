"""Render a clip to a 1080x1920 short with ffmpeg.

Captions and the hook card are drawn with Pillow and composited with ffmpeg's
`overlay` filter, so this works on ffmpeg builds without libass/drawtext
(Homebrew's default ffmpeg has neither).

Framing:
  crop  fill the frame, crop horizontally at `crop_x` (0 = left, 0.5 = centre, 1 = right)
  blur  whole frame fitted to the width over a blurred fill (safe for two-person shots)
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .transcript import Transcript, Word

W, H = 1080, 1920
CAPTION_Y = int(H * 0.66)
HOOK_Y = 220
HOOK_SECONDS = 3.5

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


@dataclass
class Caption:
    text: str
    start: float  # relative to clip start
    end: float


# System bold fonts have no emoji glyphs (they render as boxes), so burned-in
# text drops them; the post captions keep them.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def burnable(text: str) -> str:
    return " ".join(_EMOJI.sub("", text).split())


def _font(size: int, override: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in [override, *FONT_CANDIDATES]:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def caption_chunks(words: list[Word], clip_start: float, clip_end: float,
                   max_words: int = 3, max_span: float = 1.4) -> list[Caption]:
    inside = [w for w in words if w.e > clip_start and w.s < clip_end]
    chunks: list[list[Word]] = []
    for w in inside:
        cur = chunks[-1] if chunks else None
        if (cur is None or len(cur) >= max_words or w.s - cur[0].s > max_span
                or w.s - cur[-1].e > 0.6 or cur[-1].w.rstrip().endswith((".", "?", "!"))):
            chunks.append([w])
        else:
            cur.append(w)
    dur = clip_end - clip_start
    caps: list[Caption] = []
    for i, ch in enumerate(chunks):
        start = max(ch[0].s - clip_start, 0.0)
        end = ch[-1].e - clip_start
        if i + 1 < len(chunks):
            nxt = chunks[i + 1][0].s - clip_start
            if nxt - end < 0.35:  # bridge short gaps so captions don't flicker
                end = nxt
        caps.append(Caption(" ".join(x.w.strip() for x in ch), round(start, 3), round(min(end, dur), 3)))
    return caps


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        trial = f"{lines[-1]} {word}" if lines else word
        if lines and draw.textlength(trial, font=font) <= max_width:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines


def caption_png(text: str, path: Path, font_path: str | None = None) -> None:
    font = _font(78, font_path)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = _wrap(probe, burnable(text), font, W - 140)
    line_h = int(font.size * 1.18)
    img = Image.new("RGBA", (W, line_h * len(lines) + 30), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((W // 2, 12 + i * line_h), line, font=font, anchor="ma",
                  fill=(255, 255, 255, 255), stroke_width=7, stroke_fill=(0, 0, 0, 255))
    img.save(path)


def hook_png(text: str, path: Path, font_path: str | None = None) -> None:
    font = _font(58, font_path)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = _wrap(probe, burnable(text), font, W - 240)
    line_h = int(font.size * 1.25)
    box_w = int(max(probe.textlength(l, font=font) for l in lines)) + 80
    box_h = line_h * len(lines) + 50
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=28, fill=(255, 255, 255, 240))
    for i, line in enumerate(lines):
        draw.text((box_w // 2, 25 + i * line_h), line, font=font, anchor="ma", fill=(10, 10, 10, 255))
    img.save(path)


def build_command(src: str, out: Path, start: float, end: float, framing: str, crop_x: float,
                  overlays: list[tuple[Path, float, float, int]]) -> list[str]:
    dur = end - start
    if framing == "blur":
        base = ("[0:v]split[a][b];"
                f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=30[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    else:
        x = min(max(crop_x, 0.0), 1.0)
        base = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H}:(iw-ow)*{x:.3f}:(ih-oh)/2,setsar=1[v0]")
    parts = [base]
    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    for i, (png, a, b, y) in enumerate(overlays, start=1):
        inputs += ["-i", str(png)]
        parts.append(f"[v{i-1}][{i}:v]overlay=x=(W-w)/2:y={y}:enable='between(t,{a:.3f},{b:.3f})'[v{i}]")
    last = f"[v{len(overlays)}]"
    return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
            "-filter_complex", ";".join(parts), "-map", last, "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]


def render(src: str, out: Path, start: float, end: float, transcript: Transcript | None,
           framing: str = "crop", crop_x: float = 0.5, captions: bool = True,
           hook_text: str | None = None, font_path: str | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clipper-") as tmp:
        tmpdir = Path(tmp)
        overlays: list[tuple[Path, float, float, int]] = []
        if hook_text and burnable(hook_text):
            p = tmpdir / "hook.png"
            hook_png(hook_text, p, font_path)
            overlays.append((p, 0.0, min(HOOK_SECONDS, end - start), HOOK_Y))
        if captions and transcript is not None:
            for i, cap in enumerate(caption_chunks(transcript.words, start, end)):
                p = tmpdir / f"cap{i:04d}.png"
                caption_png(cap.text, p, font_path)
                overlays.append((p, cap.start, cap.end, CAPTION_Y))
        cmd = build_command(src, out, start, end, framing, crop_x, overlays)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-800:]}")
    return out
