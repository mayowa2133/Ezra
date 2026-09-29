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
        sfx=[{"type": "impact", "t": 1.2, "db": -8}], black=0.5, base=str(tmp_path))
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


def test_a_shot_never_runs_past_its_usable_footage():
    s = story.Shot("a.mp4", 105.72, 0.0, speed=0.6, src_out=106.48)
    assert abs(story.effective_speed(s, 1.0) - 0.6) < 1e-9          # 0.6 s of footage fits
    assert abs(story.effective_speed(s, 3.0) - 0.76 / 3.0) < 1e-6   # 3 s would run 1.8 s: slowed to fit
    assert story.effective_speed(story.Shot("a.mp4", 0, 0, speed=0.7), 5.0) == 0.7
