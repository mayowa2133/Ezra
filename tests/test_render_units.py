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
    assert layout.plan(none, 10, [], "auto", "9:16", 2)[0].mode == "center"   # vertical output always fills
    assert layout.plan(none, 10, [], "auto", "16:9", 2)[0].mode == "blur"     # (landscape keeps it whole)
    assert layout.plan(one, 10, [], "auto", "9:16", 2, crop_x=0.1)[0].mode == "static"
    mixed = layout.plan(one[:10] + [(t / 2, []) for t in range(10, 20)], 10, [5.0], "auto", "9:16", 2)
    assert [m.mode for m in mixed] == ["track", "center"] and layout.summary(mixed) == "mixed:center,track"
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
    orig = comp.sample_faces_and_motion
    comp.sample_faces_and_motion = lambda *a, **k: (faces, [])
    try:
        spec = RenderSpec(layout="split", caption_theme="clean", remove_silence=False, remove_fillers=False,
                          hook_text="Hook card", thumbnail=False)
        res = compose(src, 0.0, 6.0, ws, [], spec, tmp_path / "out.mp4", lambda k: Path(k), detector=Fixed())
    finally:
        comp.sample_faces_and_motion = orig
    info = probe(res.video)
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert abs(info["duration"] - 6.0) < 0.2 and res.layout == "split"
    top = _mean(res.video, 4.5, tmp_path, (0, 0, 1080, 700))
    bottom = _mean(res.video, 4.5, tmp_path, (0, 1250, 1080, 1920))
    assert top[0] > 180 and top[2] < 80, top
    assert bottom[2] > 180 and bottom[0] < 80, bottom
    assert res.srt and res.srt.read_text().count("-->") == 2   # 10 words in 6-word subtitle chunks


def test_refine_boundaries_moves_cuts_to_the_quiet_gap():
    from ezra.render import edl
    from ezra.transcription.base import Word

    # previous word audibly ends at 10.05 although ASR says 9.95; next word starts at 10.20
    words = [Word("everyone.", 9.4, 9.95), Word("What", 10.2, 10.4), Word("now?", 10.45, 10.9),
             Word("Next", 11.4, 11.7)]
    times = [round(9.8 + i * 0.005, 4) for i in range(400)]
    loud = [(9.4, 10.05), (10.2, 10.9), (11.4, 11.7)]
    energy = [1000.0 if any(a <= t <= b for a, b in loud) else 5.0 for t in times]
    pieces = [edl.Piece(9.95, 11.0)]            # snap: prev word end .. last word end + pad
    out, moved = edl.refine_boundaries(pieces, words, times, energy)
    assert moved >= 1
    assert 10.05 < out[0].src_start <= 10.2      # the tail of "everyone." is no longer in the clip
    assert 10.9 <= out[0].src_end < 11.4         # never into the next word
    # an already-quiet boundary stays put
    quiet = [5.0] * len(times)
    same, n = edl.refine_boundaries(pieces, words, times, quiet)
    assert n == 0 and same[0].src_start == 9.95


def test_split_tokens_become_whole_words():
    from ezra.transcription.base import Word, join_words, merge_split_tokens

    toks = [Word("I", 0, .1, .9), Word("lost", .1, .4, .9), Word("$400", .4, .8, .8), Word(",000", .8, 1.0, .6),
            Word("and", 1.1, 1.2), Word("my", 1.2, 1.3), Word("co", 1.3, 1.5, .9), Word("-founder", 1.5, 1.9, .7),
            Word("-", 2.0, 2.1), Word("40", 2.2, 2.4), Word("%", 2.4, 2.5)]
    merged = merge_split_tokens(toks)
    assert [w.w for w in merged] == ["I", "lost", "$400,000", "and", "my", "co-founder", "-", "40%"]
    money = merged[2]
    assert (money.s, money.e, money.p) == (.4, 1.0, .6)
    assert join_words(["billion", "-dollar", "outcomes", "-", "really"]) == "billion-dollar outcomes - really"


def test_edl_keeps_wordless_action_and_only_cuts_real_silence():
    from ezra.render import edl
    from ezra.transcription.base import Word

    words = [Word("Go!", 0.0, 0.4), Word("They", 6.0, 6.3), Word("escaped.", 6.3, 7.0),
             Word("Then", 10.0, 10.3), Word("nothing.", 10.3, 11.0)]
    # 0.4-6.0 is a chase under music (not quiet); 7.0-10.0 is real silence
    pieces, summary = edl.build(words, 0.0, 11.0, True, 0.6, True, quiet=[(7.0, 10.0)])
    kept = sum(p.length for p in pieces)
    assert kept > 7.5                                  # the 5.6 s chase stays
    assert 2.5 < summary["removed_seconds"] < 3.0      # only the silence (minus the kept pause) goes
    # without silence data every long gap is treated as dead air (talk-only behaviour)
    _, legacy = edl.build(words, 0.0, 11.0, True, 0.6, True)
    assert legacy["removed_seconds"] > 8


def test_abbreviations_do_not_end_sentences():
    from ezra.transcription.base import Word, ends_sentence, resegment

    assert not ends_sentence("Mr.") and not ends_sentence("vs.") and ends_sentence("done.")
    segs = resegment([Word("a", 0, .2), Word("Mr.", .2, .5), Word("Beast", .5, .8), Word("video.", .8, 1.1)])
    assert len(segs) == 1 and segs[0].text == "a Mr. Beast video."


def test_faceless_scenes_follow_concentrated_motion_and_groups_follow_a_face():
    from ezra.analysis.faces import Face
    from ezra.render import layout

    t = [i / 2 for i in range(20)]
    runner = [(x, 0.8, 0.8) for x in t]                       # someone running on the right third
    pan = [(x, 0.5, 0.3) for x in t]                          # motion everywhere: aerial pan / graphic
    none: list = [(x, []) for x in t]
    p = layout.plan(none, 10, [], "auto", "9:16", 2.0, motion=runner)
    assert p[0].mode == "track" and abs(p[0].shots[0].x - 0.8) < 0.05
    assert layout.plan(none, 10, [], "auto", "9:16", 2.0, motion=pan)[0].mode == "center"  # action fills
    static = [(x, None, 0.0) for x in t]                      # a text slide / graphic: nothing moves
    assert layout.plan(none, 10, [], "auto", "9:16", 2.0, motion=static)[0].mode == "blur"  # long: whole
    short = layout.plan(none[:4], 2.0, [], "auto", "9:16", 2.0, motion=static[:4])[0]
    assert short.mode == "center" and short.shots[0].x == 0.5          # a brief one still fills
    group = [(x, [Face(0.2, .5, .08, .1), Face(0.5, .5, .15, .2), Face(0.8, .5, .08, .1)]) for x in t]
    g = layout.plan(group, 10, [], "auto", "9:16", 2.0)
    assert g[0].mode == "track" and abs(g[0].shots[0].x - 0.5) < 0.05     # the biggest (nearest) face
    crowd = [(x, [Face(0.1, .5, .1, .1), Face(0.6, .5, .1, .1), Face(0.66, .5, .1, .1), Face(0.72, .5, .1, .1)])
             for x in t]
    c = layout.plan(crowd, 10, [], "auto", "9:16", 2.0)[0]
    assert c.mode == "track" and abs(c.shots[0].x - 0.66) < 0.03       # vertical: the densest cluster
    assert layout.plan(crowd, 10, [], "auto", "16:9", 2.0)[0].mode == "blur"   # landscape: show everyone


def test_active_speaker_is_framed_over_the_biggest_face():
    import numpy as np

    from ezra.analysis.faces import Face, _patches, _talk
    from ezra.render import layout

    big_quiet = Face(0.3, 0.5, 0.2, 0.3, talk=0.001)
    small_talking = Face(0.75, 0.5, 0.1, 0.15, talk=0.03)
    assert layout._subject([big_quiet, small_talking]) is small_talking
    both_quiet = [Face(0.3, 0.5, 0.2, 0.3), Face(0.75, 0.5, 0.1, 0.15)]
    assert layout._subject(both_quiet) is both_quiet[0]                    # nobody talking: most prominent
    # talk = mouth change net of upper-face change (a moving head doesn't count as talking)
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (200, 200)).astype(np.uint8)
    f = Face(0.5, 0.5, 0.4, 0.5)
    moved_mouth = frame.copy()
    moved_mouth[115:145, :] = 255 - moved_mouth[115:145, :]                  # only the lower face changes
    prev = [(f, _patches(frame, f))]
    assert _talk(prev, f, _patches(moved_mouth, f)) > 0.1
    shifted = np.roll(frame, 3, axis=1)                                    # whole head moves
    assert _talk(prev, f, _patches(shifted, f)) < 0.05


def test_caption_emoji_above_illustratable_words():
    import numpy as np
    import pytest

    from ezra.render import captions
    from ezra.transcription.base import Word

    assert captions.emoji_for(["They", "caught", "him"]) == "🚨"
    assert captions.emoji_for(["$400,000"]) == "💰" and captions.emoji_for(["the", "end"]) is None
    if captions.emoji_image("💰", 64) is None:
        pytest.skip("no colour emoji font on this system")
    words = [Word("won", 0.0, 0.5), Word("$500,000", 0.5, 1.2)]
    on = captions.CaptionRenderer(words, "pop", 1080, 1920, {}, emoji=True)
    off = captions.CaptionRenderer(words, "pop", 1080, 1920, {}, emoji=False)
    a = np.frombuffer(on.frame(0.2), np.uint8).reshape(on.band_h, 1080, 4)
    b = np.frombuffer(off.frame(0.2), np.uint8).reshape(off.band_h, 1080, 4)
    top = slice(0, on.band_h // 3)
    colourful = lambda x: int(((np.ptp(x[top, :, :3].astype(int), axis=2) > 60) & (x[top, :, 3] > 0)).sum())
    assert colourful(a) > 500 and colourful(b) == 0            # a colour glyph above the words, only when on


def test_lulls_are_quiet_but_loud_action_is_not():
    from ezra.analysis import lulls

    series = [-6.0] * 10 + [-9.5, -9.8, -9.6] + [-6.0] * 5 + [-3.0, -2.5, -2.8] + [-6.0] * 5 + [-9.0]
    loud = {"per_second": series, "median": -6.0, "p10": -9.0, "p90": -4.0}
    assert lulls(loud) == [(10.0, 13.0)]        # the 3 s dip; the loud burst and the 1 s dip at the end stay
    assert lulls(None) == []
