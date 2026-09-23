import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from ezra.analysis.faces import Face
from ezra.render import captions, edl, layout
from ezra.render.compose import compose, probe
from ezra.render.spec import RenderSpec
from ezra.transcription.base import Word, join_words, resegment, snap


def words_from(text: str, start: float = 0.0, step: float = 0.4, gaps: dict[int, float] | None = None) -> list[Word]:
    out, t = [], start
    for i, w in enumerate(text.split()):
        t += (gaps or {}).get(i, 0.0)
        out.append(Word(w, round(t, 3), round(t + 0.3, 3)))
        t += step
    return out


def test_join_words_and_resegment():
    assert join_words(["I", "lost", "$400", ",000", "40", "%", "overnight."]) == "I lost $400,000 40% overnight."
    segs = resegment(words_from("I lost it all. Then I rebuilt it slowly"))
    assert [s.text for s in segs] == ["I lost it all.", "Then I rebuilt it slowly"]
    segs = resegment([Word("one", 0, 0.3, spk="S1"), Word("two", 0.35, 0.6, spk="S2")])
    assert len(segs) == 1  # speaker change without a pause does not split a sentence


def test_snap_pads_without_entering_neighbours():
    ws = words_from("alpha beta gamma delta")
    s, e = snap(ws, 0.5, 1.0)
    assert s >= ws[0].e and e <= ws[3].s
    with pytest.raises(ValueError):
        snap(ws, 100, 101)


def test_edl_removes_fillers_and_long_pauses_and_remaps():
    ws = words_from("so um this is uh the point", gaps={4: 2.0})
    pieces, summary = edl.build(ws, 0.0, ws[-1].e + 0.2, True, 0.6, True)
    assert summary["removed_fillers"] == 2 and summary["removed_silence"] > 1.5
    out = edl.remap_words(ws, pieces)
    assert [w.w for w in out] == ["so", "this", "is", "the", "point"]
    assert all(b.s >= a.e - 1e-6 for a, b in zip(out, out[1:]))
    assert edl.output_duration(pieces) < ws[-1].e - 1.5
    none, s2 = edl.build(ws, 0.0, 5.0, False, 0.6, False)
    assert len(none) == 1 and s2["removed_seconds"] == 0


def test_caption_chunks_themes_and_sidecars():
    ws = words_from("He lost four hundred thousand dollars overnight. Nobody warned him about it.")
    chunks = captions.chunk_words(ws, 3)
    assert all(len(c.words) <= 3 for c in chunks)
    assert all(b.start >= a.start for a, b in zip(chunks, chunks[1:]))
    for theme in captions.THEMES:
        r = captions.CaptionRenderer(ws, theme, 1080, 1920, {"top": 0.1, "bottom": 0.2, "left": 0.05, "right": 0.05})
        frame = r.frame(0.5)
        assert len(frame) == 1080 * r.band_h * 4
        img = Image.frombytes("RGBA", (1080, r.band_h), frame)
        assert img.getbbox() is not None, theme          # something was drawn
        assert r.frame(100.0) == bytes(len(frame))       # nothing after the last word
    srt = captions.to_srt(ws)
    assert srt.startswith("1\n00:00:00,000 --> ") and "overnight." in srt
    ass = captions.to_ass(ws, "karaoke", 1080, 1920)
    assert "[V4+ Styles]" in ass and "{\\k" in ass
    with pytest.raises(ValueError):
        captions.CaptionRenderer(ws, "nope", 1080, 1920, {})


def test_burnable_strips_emoji():
    assert captions.burnable("He lost $400k 😳🤯") == "He lost $400k"


def test_layout_plan_modes():
    one = [(t / 2, [Face(0.3, 0.4, 0.1, 0.15)]) for t in range(20)]
    two = [(t / 2, [Face(0.25, 0.4, 0.05, 0.1), Face(0.75, 0.4, 0.05, 0.1)]) for t in range(20)]
    none = [(t / 2, []) for t in range(20)]
    assert layout.plan(one, 10, [], "auto", "9:16", 2)[0].mode == "track"
    p = layout.plan(two, 10, [], "auto", "9:16", 2)[0]
    assert p.mode == "split" and p.split_x == (0.25, 0.75)
    assert layout.plan(two, 10, [], "auto", "16:9", 2)[0].mode == "track"   # split is vertical-only
    assert layout.plan(none, 10, [], "auto", "9:16", 2)[0].mode == "blur"
    assert layout.plan(one, 10, [], "auto", "9:16", 2, crop_x=0.1)[0].mode == "static"
    mixed = layout.plan(one[:10] + [(t / 2, []) for t in range(10, 20)], 10, [5.0], "auto", "9:16", 2)
    assert [m.mode for m in mixed] == ["track", "blur"] and layout.summary(mixed) == "mixed:blur,track"
    assert layout.x_expr([(0, 4, 0.3), (4, 8, 0.7)]).startswith("if(lt(t,4.000)")
    assert "ih-oh" in layout.x_expr([(0, 1, 0.5)], axis="y")


def test_shot_planning_ignores_single_misdetections():
    samples = [(t / 2, [Face(x, 0.4, 0.1, 0.1)]) for t, x in enumerate([0.3, 0.3, 0.9, 0.3, 0.3, 0.3])]
    shots = layout.plan_shots(samples, 0, 3, 2)
    assert len(shots) == 1 and shots[0].x == pytest.approx(0.3)
    cut = [(t / 2, [Face(0.3 if t < 8 else 0.75, 0.4, 0.1, 0.1)]) for t in range(16)]
    assert [(s.start, s.end) for s in layout.plan_shots(cut, 0, 8, 2)] == [(0, 4.0), (4.0, 8)]


def _mean(video: Path, t: float, tmp: Path, box=None) -> tuple[int, ...]:
    png = tmp / f"f{t}.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(png)],
                   check=True)
    img = Image.open(png).convert("RGB")
    if box:
        img = img.crop(box)
    return tuple(int(v) for v in ImageStat.Stat(img).mean)


def test_real_render_split_layout_with_captions(tmp_path):
    """Two coloured halves, forced split: top half must show the left colour,
    bottom half the right one; captions and loudness filters must not break it."""
    src = tmp_path / "two.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:size=640x720:rate=25",
                    "-f", "lavfi", "-i", "color=c=blue:size=640x720:rate=25", "-f", "lavfi",
                    "-i", "sine=frequency=300:sample_rate=44100", "-filter_complex", "[0][1]hstack[v]",
                    "-map", "[v]", "-map", "2:a", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(src)], check=True)
    ws = words_from("one two three four five six seven eight nine ten", step=0.5)
    faces = [(t / 2, [Face(0.25, 0.5, 0.1, 0.1), Face(0.75, 0.5, 0.1, 0.1)]) for t in range(12)]

    class Fixed:
        name = "fixed"

    import sys
    comp = sys.modules["ezra.render.compose"]   # the package re-exports a function named `compose`
    orig = comp.sample_faces
    comp.sample_faces = lambda *a, **k: faces
    try:
        spec = RenderSpec(layout="split", caption_theme="clean", remove_silence=False, remove_fillers=False,
                          hook_text="Hook card", thumbnail=False)
        res = compose(src, 0.0, 6.0, ws, [], spec, tmp_path / "out.mp4", lambda k: Path(k), detector=Fixed())
    finally:
        comp.sample_faces = orig
    info = probe(res.video)
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert abs(info["duration"] - 6.0) < 0.2 and res.layout == "split"
    top = _mean(res.video, 4.5, tmp_path, (0, 0, 1080, 700))
    bottom = _mean(res.video, 4.5, tmp_path, (0, 1250, 1080, 1920))
    assert top[0] > 180 and top[2] < 80, top
    assert bottom[2] > 180 and bottom[0] < 80, bottom
    assert res.srt and res.srt.read_text().count("-->") == 2   # 10 words in 6-word subtitle chunks
