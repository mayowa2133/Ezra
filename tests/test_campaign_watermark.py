"""Locked campaign watermark, bleeps and caption fixes: placement rules and a real render checked
frame by frame."""

import hashlib
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from ezra.render import captions
from ezra.render.compose import compose
from ezra.render.spec import CampaignWatermark, RenderSpec, WordFix
from ezra.render.watermark import placement, visible_bbox
from ezra.transcription.base import Word

W, H = 1080, 1920


def _mark(path: Path, opaque: bool = False) -> Path:
    """A campaign-style mark: white text with a soft dark glow on a transparent canvas."""
    im = Image.new("RGBA" if not opaque else "RGB", (1200, 400), (0, 0, 0, 0) if not opaque else (255, 255, 255))
    d = ImageDraw.Draw(im)
    font = ImageFont.truetype(captions.find_font("heavy") or "", 150)
    d.text((300, 110), "YT: @test", fill=(255, 255, 255, 255) if not opaque else (250, 250, 250), font=font,
           stroke_width=6, stroke_fill=(0, 0, 0, 90) if not opaque else (200, 200, 200))
    im.save(path)
    return path


def test_placement_refuses_corners_rail_captions_and_offframe(tmp_path):
    m = _mark(tmp_path / "m.png")
    size, bbox = Image.open(m).size, visible_bbox(m)
    ok = placement(size, bbox, (W, H), (0.5, 0.6), 0.34, [("captions", (0, 1250, W, 1500))])
    assert abs(ok["width"] / ok["height"] - size[0] / size[1]) < 0.01          # uniform scale only
    for center, why in [((0.12, 0.05), "corner"), ((0.9, 0.6), "right-side"), ((0.5, 0.999), "off the frame"),
                        ((0.5, 0.72), "captions"), ((0.5, 0.93), "bottom caption/username")]:
        with pytest.raises(ValueError, match=why):
            placement(size, bbox, (W, H), center, 0.34, [("captions", (0, 1250, W, 1500))])
    with pytest.raises(ValueError, match="too small"):
        placement(size, bbox, (W, H), (0.5, 0.6), 0.06, [])


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x305070:size=1920x1080:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000", "-t", "8", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    return src


def _words() -> list[Word]:
    text = "they started chinning fuck Trayon you know what I'm saying so".split()
    return [Word(w, 0.5 + i * 0.6, 0.5 + i * 0.6 + 0.45, 0.9) for i, w in enumerate(text)]


def _spec(mark: Path, **kw) -> RenderSpec:
    base = dict(layout="center", remove_silence=False, remove_fillers=False, thumbnail=False, hook_overlay=True,
                hook_text="TRAE SAYS KNICKS FANS STARTED THE RIVALRY", normalize_audio=False,
                campaign_watermark=CampaignWatermark(key=str(mark), center_x=0.5, center_y=0.30, visible_width=0.34,
                                                     sha256=hashlib.sha256(mark.read_bytes()).hexdigest()),
                caption_fixes=[WordFix(at=1.7, text="chanting,"), WordFix(at=2.9, text="Trae Young.")],
                bleep=[2.3])
    base.update(kw)
    return RenderSpec(**base)


def test_render_locks_the_watermark_on_every_frame_bleeps_and_fixes_captions(tmp_path):
    mark = _mark(tmp_path / "mark.png")
    res = compose(_source(tmp_path), 0.0, 7.0, _words(), [], _spec(mark), tmp_path / "out.mp4", lambda k: Path(k))
    wm = res.edit_summary["campaign_watermark"]
    assert wm["sha256"] == hashlib.sha256(mark.read_bytes()).hexdigest()
    # expected pixels: the mark scaled uniformly and composited over the plain background colour
    scaled = Image.open(mark).resize((wm["width"], wm["height"]), Image.LANCZOS)
    vx0, vy0, vx1, vy1 = wm["visible"]
    x0, y0 = int(vx0) // 2 * 2, int(vy0) // 2 * 2                 # even crop box (4:2:0 video)
    x1, y1 = -(-int(vx1 + 1) // 2) * 2, -(-int(vy1 + 1) // 2) * 2
    # every frame, the visible watermark region matches the expected composite
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(res.video), "-vf", f"crop={x1 - x0}:{y1 - y0}:{x0}:{y0}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, y1 - y0, x1 - x0, 3).astype(float)
    # the background colour as the encoder rendered it (YUV conversion shifts it slightly)
    first = subprocess.run(["ffmpeg", "-v", "error", "-i", str(res.video), "-vf", f"crop=40:40:20:{y0}",
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True, check=True).stdout
    colour = tuple(int(v) for v in np.frombuffer(first, np.uint8).reshape(-1, 3).mean(0).round())
    bg = Image.new("RGBA", (wm["width"], wm["height"]), (*colour, 255))
    bg.alpha_composite(scaled)
    expect = np.asarray(bg.convert("RGB")).astype(float)[y0 - wm["y"]:y1 - wm["y"], x0 - wm["x"]:x1 - wm["x"]]
    # compare brightness (chroma is subsampled by the encoder): the pattern must match on every frame
    luma = frames @ np.array([0.299, 0.587, 0.114])
    exp_luma = expect @ np.array([0.299, 0.587, 0.114])
    corr = [float(np.corrcoef(f.ravel(), exp_luma.ravel())[0, 1]) for f in luma]
    diff = np.abs(luma - exp_luma).mean(axis=(1, 2))
    assert len(frames) >= 7 * 30 - 1, len(frames)
    assert min(corr) > 0.97 and diff.max() < 8.0, (min(corr), diff.max())
    # captions: fixed spellings, the bleeped word masked; the sidecar agrees
    srt = res.srt.read_text()
    assert "chanting," in srt and "Trae Young." in srt and "f***" in srt and "fuck" not in srt
    # audio: a 1 kHz tone where the word was, the original 300 Hz elsewhere
    a, b = res.edit_summary["bleeps"][0]

    def peak(t0: float, t1: float) -> float:
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(res.video),
                              "-ac", "1", "-ar", "48000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
        x = np.frombuffer(pcm, np.int16).astype(float)
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        return float(np.fft.rfftfreq(len(x), 1 / 48000)[spec.argmax()])

    assert abs(peak(a + 0.05, b - 0.05) - 1000) < 30 and abs(peak(0.1, 1.5) - 300) < 30


def test_render_refuses_an_opaque_or_changed_watermark_and_unknown_words(tmp_path):
    src = _source(tmp_path)
    opaque = _mark(tmp_path / "opaque.png", opaque=True)
    with pytest.raises(ValueError, match="no transparency"):
        compose(src, 0.0, 7.0, _words(), [], _spec(opaque), tmp_path / "a.mp4", lambda k: Path(k))
    mark = _mark(tmp_path / "mark.png")
    changed = _spec(mark)
    changed.campaign_watermark.sha256 = "0" * 64
    with pytest.raises(ValueError, match="changed"):
        compose(src, 0.0, 7.0, _words(), [], changed, tmp_path / "b.mp4", lambda k: Path(k))
    with pytest.raises(ValueError, match=r"no word starts at 5\.00s"):
        compose(src, 0.0, 7.0, _words(), [], _spec(mark, bleep=[5.0]), tmp_path / "c.mp4", lambda k: Path(k))
