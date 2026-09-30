"""Narrated sports stories (ezra.storytelling): alignment, the Voicebox client, mixing and a real render."""

import json
import subprocess
import wave

import numpy as np

from ezra.render import captions as cap
from ezra.storytelling import narration, voicebox
from ezra.storytelling import render as story


def test_alignment_keeps_script_spelling_and_spreads_merged_words():
    heard = [("Did", 0.0, 0.2), ("Trey", 0.2, 0.5), ("Young", 0.5, 0.8), ("scored", 0.8, 1.2), ("22", 1.2, 1.8)]
    out = narration._align(["Did", "Trae", "Young", "scored", "twenty", "two."], heard, 2.0)
    assert [w.w for w in out] == ["Did", "Trae", "Young", "scored", "twenty", "two."]
    assert out[1].s == 0.2 and out[1].e == 0.5                 # "Trae" takes "Trey"'s time
    assert out[4].s == 1.2 and abs(out[5].e - 1.8) < 1e-6        # "22" spread over "twenty two."
    assert all(b.s >= a.s for a, b in zip(out, out[1:]))


def test_trim_drops_tts_padding():
    sr = narration.SR
    x = np.concatenate([np.zeros(sr // 2), 0.3 * np.sin(np.arange(sr) / 5), np.zeros(sr)]).astype(np.float32)
    y = narration.trim(x)
    assert abs(len(y) / sr - 1.06) < 0.03


def test_voicebox_speak_polls_and_caches(tmp_path, monkeypatch):
    calls = []
    wav = tmp_path / "src.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 2400)
    states = iter(["generating", "completed"])

    def fake(method, path, body=None, timeout=60):
        calls.append((method, path))
        if path == "/profiles" and method == "GET":
            return [{"id": "p1", "preset_engine": "kokoro", "preset_voice_id": "bf_emma"}]
        if path == "/generate":
            assert body["profile_id"] == "p1" and body["engine"] == "kokoro"
            return {"id": "g1"}
        if path == "/history/g1":
            return {"status": next(states)}
        if path.endswith("export-audio"):
            return wav.read_bytes()
        raise AssertionError(path)

    monkeypatch.setattr(voicebox, "_call", fake)
    monkeypatch.setattr(voicebox.time, "sleep", lambda s: None)
    out = voicebox.speak("Hello there.", tmp_path / "a.wav", voice="bf_emma")
    assert out.read_bytes() == wav.read_bytes()
    n = len(calls)
    voicebox.speak("Hello there.", tmp_path / "b.wav", voice="bf_emma")      # cached: no new calls
    assert len(calls) == n


def _eval_gain(expr: str, t: float) -> float:
    # evaluate the ffmpeg volume expression in Python (if/lt only)
    py = expr.replace("if(", "_if(").replace("lt(", "_lt(")
    return eval(py, {"_if": lambda c, a, b: a if c else b, "_lt": lambda a, b: a < b, "t": t})


def test_gain_expression_is_piecewise_linear_in_db_points():
    e = story._gain_expr([[0, -20], [10, 0], [12, -40]])
    assert abs(_eval_gain(e, 0) - 0.1) < 1e-4
    assert abs(_eval_gain(e, 10) - 1.0) < 1e-4
    assert abs(_eval_gain(e, 5) - (0.1 + 0.9 * 0.5)) < 1e-4
    assert abs(_eval_gain(e, 20) - 0.01) < 1e-4


def test_sfx_are_where_they_should_be():
    x = story.synth_sfx([{"type": "impact", "t": 1.0, "db": -6},
                         {"type": "riser", "t": 3.0, "dur": 1.0, "db": -12}], 4.0)
    rms = lambda a, b: float(np.sqrt(np.mean(x[int(a * story.AR):int(b * story.AR)] ** 2)))
    assert rms(0.0, 0.9) < 1e-6 < rms(1.0, 1.3)          # silence, then the hit
    assert rms(2.0, 2.4) < rms(2.6, 3.0)                  # the riser builds into its end


def test_story_theme_only_colours_chosen_words():
    assert not cap.is_keyword("money", set(), lexicon=False)
    assert cap.is_keyword("Villain.", {"villain"}, lexicon=False)
    assert cap.is_keyword("money", set())                  # other themes keep the lexicon


def test_render_story_timeline(tmp_path):
    for name, colour in (("a.mp4", "0x803020"), ("b.mp4", "0x205080")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=4",
                        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:d=4",
                        "-vf", f"drawbox=c={colour}@0.3:t=fill", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(tmp_path / name)], check=True)
    sr = 24000
    with wave.open(str(tmp_path / "nar.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((0.3 * np.sin(2 * np.pi * 220 * np.arange(2 * sr) / sr) * 32767).astype(np.int16).tobytes())
    tl = story.Timeline(
        shots=[story.Shot("a.mp4", 0.5, 0.0, speed=0.5, mode="full", focus="center"),
               story.Shot("b.mp4", 1.0, 1.2, speed=1.0, mode="band", focus="center", audio_db=-10)],
        end=2.5, narration="nar.wav", words=[{"w": "New", "s": 0.1, "e": 0.4}, {"w": "York.", "s": 0.5, "e": 0.9}],
        sfx=[{"type": "impact", "t": 1.2, "db": -8}], black=0.5, base=str(tmp_path),
        titles=[{"text": "Everybody acts tough", "s": 1.5, "e": 2.4, "y": 0.4}])
    r = story.render(tl, tmp_path / "out.mp4")
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                                       "stream=codec_type,width,height,nb_read_frames:format=duration", "-of", "json",
                                       str(tmp_path / "out.mp4")], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert int(v["nb_read_frames"]) == 90                  # 2.5 s + 0.5 s black at 30 fps
    assert any(s["codec_type"] == "audio" for s in probe["streams"])
    assert r["shots"] == 2 and abs(r["duration"] - 3.0) < 0.05
    # the caption is drawn: bright pixels in the caption band at 0.3 s
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.3", "-i", str(tmp_path / "out.mp4"), "-frames:v", "1",
                          "-vf", "crop=1080:200:0:1244,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    assert (np.frombuffer(raw, np.uint8) > 235).mean() > 0.005

    # the title card is on screen at 2.0 s (bright text around 40% height) and gone at 2.7 s
    def bright(t: float) -> float:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(tmp_path / "out.mp4"),
                              "-frames:v", "1", "-vf", "crop=1080:160:0:688,format=gray", "-f", "rawvideo", "-"],
                             capture_output=True).stdout
        return float((np.frombuffer(raw, np.uint8) > 235).mean())
    assert bright(2.0) > 0.01 and bright(2.7) < bright(2.0) / 4


def test_a_shot_never_runs_past_its_usable_footage():
    s = story.Shot("a.mp4", 105.72, 0.0, speed=0.6, src_out=106.48)
    assert abs(story.effective_speed(s, 1.0) - 0.6) < 1e-9          # 0.6 s of footage fits
    assert abs(story.effective_speed(s, 3.0) - 0.76 / 3.0) < 1e-6   # 3 s would run 1.8 s: slowed to fit
    assert story.effective_speed(story.Shot("a.mp4", 0, 0, speed=0.7), 5.0) == 0.7


def test_story_helpers():
    from ezra.storytelling.story import Timing, fit_music, steady_music

    tm = Timing([{"text": "Trae Young.", "start": 1.0, "end": 1.8, "words": [{"w": "Trae", "s": 1.0, "e": 1.3},
                                                                              {"w": "Young.", "s": 1.3, "e": 1.8}]}])
    assert tm.at(0, "young") == 1.22 and tm.end_of(0) == 1.8
    assert fit_music(59.63, 55.67) == {"offset": 3.96, "tempo": 1.0}
    assert fit_music(59.63, 61.34)["tempo"] == 0.9721
    g = steady_music(-4.0, 60.0, drops=[(20.0, 21.0)])
    assert [20.3, -40.0] in g and g[0] == [0.0, -4.0] and g[-1][1] == -40.0


def test_phrase_captions_keep_impact_words_alone():
    from ezra.transcription.base import Word
    ws = [Word(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(
        "This picture became a meme. But most people forget what happened before it.".split())]
    chunks = story.phrase_chunks(ws, {"meme", "forget"})
    texts = [" ".join(w.w for w in c.words) for c in chunks]
    assert "meme." in texts and "forget" in texts
    assert all(len(c.words) <= 3 for c in chunks)
    assert all(b.start >= a.start and a.end <= b.start + 1e-9 for a, b in zip(chunks, chunks[1:]))


def test_track_path_expression_interpolates():
    e = story._path_expr([(0.0, 0.2), (1.0, 0.6), (2.0, 0.6)])
    f = lambda t: eval(e.replace("if(", "_if(").replace("lt(", "_lt("),
                       {"_if": lambda c, a, b: a if c else b, "_lt": lambda a, b: a < b, "t": t})
    assert abs(f(0.5) - 0.4) < 1e-9 and abs(f(1.5) - 0.6) < 1e-9 and abs(f(-1) - 0.2) < 1e-9


def test_action_audit_flags_a_cut_before_the_play_ends(tmp_path):
    from ezra.storytelling.scout import action_audit

    # one camera shot of 2 s (the play), then a hard cut to another camera
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2", "-f", "lavfi",
                    "-i", "color=c=0x2040a0:s=640x360:r=30:d=2", "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
                    "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    str(tmp_path / "play.mp4")], check=True)
    shot = {"src": "play.mp4", "src_in": 0.0, "t": 0.0, "speed": 1.0, "action": True, "label": "the drive"}
    early = action_audit([shot], 1.0, tmp_path)          # leaves at 1.0 s; the play runs to 2.0 s
    assert len(early) == 1 and "before the play ends" in early[0]
    assert action_audit([shot], 1.9, tmp_path) == []      # stays to the outcome


def test_fit_shot_keeps_the_whole_region(tmp_path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=1280x720:r=30:d=2",
                    "-vf", "drawbox=x=400:y=0:w=480:h=720:c=red:t=fill", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(tmp_path / "v.mp4")], check=True)
    s = story.Shot("v.mp4", 0.0, 0.0, speed=1.0, mode="fit", region=[400 / 1280, 0, 880 / 1280, 1], zoom=[1.0, 1.0])
    story.render_shot(s, 1.0, tmp_path, tmp_path / "o.mp4", 30)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(tmp_path / "o.mp4"), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    img = np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3).astype(int)
    red = img[..., 0] - img[..., 1] > 40
    rows = np.nonzero(red.mean(axis=1) > 0.4)[0]
    cols = np.nonzero(red[960])[0]
    # the 480x720 region is shown whole at 2.13x (1024 x 1536: limited by 80% of the height), centred at 44%
    assert 1450 < rows[-1] - rows[0] < 1560 and abs((rows[0] + rows[-1]) / 2 / 1920 - 0.44) < 0.02
    assert 990 < cols[-1] - cols[0] < 1040


def test_source_audio_is_levelled_to_the_narration(tmp_path):
    for name, vol in (("quiet.wav", 0.02), ("nar.wav", 0.3)):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=300:d=3", "-af",
                        f"volume={vol}", "-ar", "48000", str(tmp_path / name)], check=True)
    q, n = story._lufs(["-i", str(tmp_path / "quiet.wav")]), story._lufs(["-i", str(tmp_path / "nar.wav")])
    assert q is not None and n is not None and 20 < n - q < 26          # 0.02 vs 0.3 is ~23.5 dB
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "1",
                    str(tmp_path / "silent.wav")], check=True)
    assert story._lufs(["-i", str(tmp_path / "silent.wav")]) is None


def test_audio_clips_play_across_shot_cuts(tmp_path):
    sr = 24000
    with wave.open(str(tmp_path / "nar.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * (3 * sr))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:d=4",
                    str(tmp_path / "quote.wav")], check=True)
    # a 2 s quote starting at 0.5 s runs over three silent shots of different sources
    tl = story.Timeline(shots=[], end=3.0, narration="nar.wav", words=[], base=str(tmp_path),
                        audio_clips=[{"src": "quote.wav", "src_in": 1.0, "t": 0.5, "dur": 2.0, "db": -6}])
    mix = story.mix_audio(tl, tmp_path, 3.0, tmp_path)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mix), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, np.int16).astype(float)
    rms = lambda a, b: float(np.sqrt(np.mean(x[int(a * 16000):int(b * 16000)] ** 2)))
    assert rms(0.0, 0.4) < 50 < rms(0.7, 2.3) and rms(2.7, 3.0) < 50
