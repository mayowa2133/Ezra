"""Render a narrated story timeline (JSON) to a vertical MP4.

The timeline is built around the narration: every shot has a timeline start (normally a word
onset) and a source range. Each shot is cut, speed-changed and framed on its own, with one of
two framings:
  full  9:16 crop (face-tracked with Ezra's detector when focus is "auto") with a slow push-in
  band  the landscape frame across the middle over a blurred, darkened copy of itself: the
        reference's edge-blur look, adapted to vertical, for wides
Then one shared grade (per-shot exposure normalisation, gentle S-curve, lower saturation, vignette,
grain), concatenation, a mixed soundtrack (narration, ducked music with gain automation, source
audio, synthesised risers/impacts) and one-word captions from ezra.render.captions ("story").
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..render import captions as cap
from ..transcription.base import Word

W, H, FPS = 1080, 1920, 30
AR = 48000
BAND_H = 608                     # 1080 x 608 landscape band (16:9)


@dataclass
class Shot:
    src: str
    src_in: float
    t: float                               # timeline start (s)
    speed: float = 1.0                     # <1 = slow motion
    mode: str = "full"                     # full | band
    focus: list[float] | str = "auto"      # [x, y] fractions of the source frame, "auto" or "center"
    zoom: list[float] = field(default_factory=lambda: [1.0, 1.06])   # push-in over the shot
    audio_db: float | None = None          # source audio level; None = silent
    smooth: bool = False                   # motion-interpolated slow motion
    flash: bool = False                    # white flash into the shot
    src_out: float | None = None           # end of usable footage: the shot is slowed to fit rather than run past it
    audio_len: float | None = None         # play the source audio for at most this long
    safe_top: float = 0.0                  # share of the source height kept out of frame (broadcast bugs)
    safe_bottom: float = 0.12
    label: str = ""


@dataclass
class Timeline:
    shots: list[Shot]
    end: float                             # end of the last shot (then black)
    narration: str
    words: list[dict[str, Any]]            # caption words: {w, s, e}
    music: dict[str, Any] = field(default_factory=dict)
    sfx: list[dict[str, Any]] = field(default_factory=list)
    black: float = 1.2
    keywords: list[str] = field(default_factory=list)
    keyword_color: str = "#E8B54A"
    caption_position: float = 0.70
    narration_db: float = 0.0
    base: str = "."

    @classmethod
    def load(cls, path: Path) -> Timeline:
        d = json.loads(path.read_text())
        d["shots"] = [Shot(**s) for s in d["shots"]]
        d.setdefault("base", str(path.parent))
        return cls(**d)


# --- helpers -----------------------------------------------------------------------------------

def _probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height,r_frame_rate:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    return {"w": int(v["width"]), "h": int(v["height"]), "dur": float(d["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in d["streams"])}


def _auto_focus(src: Path, t0: float, t1: float) -> list[float]:
    """Centre of the biggest faces over the shot (Ezra's detector); centre-ish if none."""
    from ..analysis.faces import sample_frames

    s = sample_frames(src, t0, max(t1, t0 + 0.3), fps=4, overlays=False)
    faces = [max(fs, key=lambda f: f.w) for _, fs in s.faces if fs]
    if not faces:
        return [0.5, 0.42]
    return [float(np.median([f.cx for f in faces])), float(np.median([f.cy for f in faces]))]


def _mean_luma(src: Path, t: float, dur: float) -> float:
    vals = []
    for k in (0.2, 0.5, 0.8):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t + dur * k:.3f}", "-i", str(src), "-frames:v", "1",
                              "-vf", "scale=64:-2,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
        if raw:
            vals.append(np.frombuffer(raw, np.uint8).mean() / 255)
    return float(np.mean(vals)) if vals else 0.4


def _grade(brightness: float) -> str:
    return (f"eq=contrast=1.10:saturation=0.80:gamma=0.96:brightness={brightness:.3f},"
            "curves=all='0/0 0.22/0.18 0.5/0.5 0.8/0.84 1/1',"
            "colorbalance=rs=-0.02:bs=0.025:rh=0.02:bh=-0.015,"
            "vignette=angle=PI/4.6,noise=alls=4:allf=t")


# --- shots -------------------------------------------------------------------------------------

def effective_speed(shot: Shot, dur: float) -> float:
    """The shot's speed, slowed if needed so it ends by `src_out`."""
    if shot.src_out is None:
        return shot.speed
    return max(0.2, min(shot.speed, (shot.src_out - shot.src_in) / max(dur, 1e-3)))


def render_shot(shot: Shot, dur: float, base: Path, out: Path, frames: int) -> dict[str, Any]:
    src = (base / shot.src).resolve()
    info = _probe(src)
    speed = effective_speed(shot, dur)
    span = dur * speed
    focus = shot.focus
    if focus == "auto":
        focus = _auto_focus(src, shot.src_in, shot.src_in + span)
    elif focus == "center":
        focus = [0.5, 0.45]
    fx, fy = float(focus[0]), float(focus[1])
    z0, z1 = shot.zoom
    top, bot = shot.safe_top, shot.safe_bottom
    zt = f"({z0}+({z1}-{z0})*t/{dur:.3f})"
    luma = _mean_luma(src, shot.src_in, span)
    bright = float(np.clip((0.36 - luma) * 0.55, -0.08, 0.10))
    pre = []
    if shot.smooth and speed < 1:
        pre.append("minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
    pre.append(f"setpts=(PTS-STARTPTS)/{speed:.4f}")
    pre.append(f"fps={FPS}")
    head = ",".join(pre)
    if shot.mode == "band":
        bg = (f"scale=-2:{H},crop={W}:{H}:(iw-{W})*{fx}:0,gblur=sigma=28,"
              "eq=brightness=-0.16:saturation=0.6")
        fg = (f"scale=w='2*trunc({W}*{zt}/2)':h=-2:eval=frame:flags=lanczos,"
              f"crop={W}:{BAND_H}:x='clip({fx}*iw-{W // 2},0,iw-{W})':"
              f"y='clip({fy}*ih-{BAND_H // 2},{top}*ih,max({top}*ih,(1-{bot})*ih-{BAND_H}))'")
        y = int(H * 0.44 - BAND_H / 2)
        vf = (f"[0:v]{head},split[a][b];[a]{bg}[bg];[b]{fg}[fg];[bg][fg]overlay=0:{y},"
              f"{_grade(bright)}")
    else:
        vf = (f"[0:v]{head},scale=w=-2:h='2*trunc({H}*{zt}/2)':eval=frame:flags=lanczos,"
              f"crop={W}:{H}:x='clip({fx}*iw-{W // 2},0,iw-{W})':"
              f"y='clip({fy}*ih-{H // 2},{top}*ih,max({top}*ih,(1-{bot})*ih-{H}))',"
              f"unsharp=5:5:0.5,{_grade(bright)}")
    vf += ",tpad=stop_mode=clone:stop_duration=10"      # never run short of frames past the source end
    if shot.flash:
        vf += ",fade=t=in:st=0:d=0.12:color=white"
    vf += ",format=yuv420p[v]"
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{shot.src_in:.3f}", "-t", f"{span + 0.6:.3f}", "-i", str(src),
           "-filter_complex", vf, "-map", "[v]", "-an", "-frames:v", str(frames), "-r", str(FPS),
           "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-g", str(FPS), str(out)]
    subprocess.run(cmd, check=True)
    return {"focus": [round(fx, 3), round(fy, 3)], "src_w": info["w"], "src_h": info["h"], "luma": round(luma, 3),
            "brightness": round(bright, 3), "speed_used": round(speed, 3)}


# --- audio -------------------------------------------------------------------------------------

def _write_wav(path: Path, x: np.ndarray) -> None:
    x = np.clip(x, -1, 1)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(AR)
        w.writeframes((x * 32767).astype(np.int16).tobytes())


def synth_sfx(events: list[dict[str, Any]], length: float) -> np.ndarray:
    """Risers, sub impacts and whooshes, synthesised (no samples, nothing licensed)."""
    rng = np.random.default_rng(3)
    out = np.zeros((int(length * AR) + AR, 2))

    def add(sig: np.ndarray, t: float, db: float) -> None:
        i = int(max(0.0, t) * AR)
        n = min(len(sig), len(out) - i)
        if n > 0:
            out[i:i + n] += sig[:n] * 10 ** (db / 20)

    for e in events:
        kind, t, db = e["type"], float(e["t"]), float(e.get("db", -6))
        if kind == "impact":
            d = float(e.get("dur", 2.2))
            n = int(d * AR)
            tt = np.arange(n) / AR
            f = 32 + 38 * np.exp(-tt * 9)                     # 70 Hz falling to 32 Hz
            sub = np.sin(2 * np.pi * np.cumsum(f) / AR) * np.exp(-tt * 1.6)
            noise = rng.normal(0, 1, n)
            # low thump: short lowpassed noise burst
            k = np.exp(-np.arange(200) / 40.0)
            thump = np.convolve(noise, k / k.sum(), "same") * np.exp(-tt * 18) * 0.9
            sig = np.stack([sub + thump, sub + thump * 0.95], axis=1)
            add(sig / 1.4, t, db)
        elif kind == "riser":
            d = float(e.get("dur", 2.0))
            n = int(d * AR)
            tt = np.arange(n) / AR
            env = (tt / d) ** 2.2
            noise = rng.normal(0, 1, n)
            # sweep: high-passed noise getting brighter (simple 1-pole filters with rising cutoff)
            y = np.zeros(n)
            a = 0.0
            for i in range(n):
                c = 0.02 + 0.5 * (tt[i] / d) ** 2
                a += c * (noise[i] - a)
                y[i] = noise[i] - a
            tone = np.sin(2 * np.pi * np.cumsum(180 + 520 * (tt / d) ** 2) / AR) * 0.25
            sig = (y * 0.35 + tone) * env
            add(np.stack([sig, sig * 0.97], axis=1), t - d, db)
        elif kind == "whoosh":
            d = float(e.get("dur", 0.45))
            n = int(d * AR)
            tt = np.arange(n) / AR
            env = np.sin(np.pi * tt / d) ** 2
            noise = rng.normal(0, 1, n)
            k = np.exp(-np.arange(60) / 12.0)
            sig = np.convolve(noise, k / k.sum(), "same") * env * 0.8
            add(np.stack([sig, sig], axis=1), t - d / 2, db)
    return out


def _gain_expr(points: list[list[float]]) -> str:
    """Piecewise-linear gain (dB) over time as an ffmpeg volume expression (linear factor)."""
    pts = sorted(points)
    expr = f"{10 ** (pts[-1][1] / 20):.5f}"
    for (t0, d0), (t1, d1) in reversed(list(zip(pts, pts[1:]))):
        g0, g1 = 10 ** (d0 / 20), 10 ** (d1 / 20)
        seg = f"({g0:.5f}+({g1:.5f}-{g0:.5f})*(t-{t0:.3f})/{max(1e-3, t1 - t0):.3f})"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return f"if(lt(t,{pts[0][0]:.3f}),{10 ** (pts[0][1] / 20):.5f},{expr})"


def mix_audio(tl: Timeline, base: Path, total: float, tdir: Path) -> Path:
    inputs = ["-i", str((base / tl.narration).resolve())]
    parts = [f"[0:a]aresample={AR},aformat=channel_layouts=stereo,volume={tl.narration_db}dB,"
             f"apad=whole_dur={total:.3f}[nar]"]
    mixes = ["[nar]"]
    n = 1
    m = tl.music
    if m:
        inputs += ["-i", str((base / m["path"]).resolve())]
        gain = _gain_expr(m.get("gain", [[0, -18], [total, -18]]))
        tempo = f"atempo={m['tempo']:.4f}," if m.get("tempo") and abs(m["tempo"] - 1) > 1e-4 else ""
        parts.append(f"[{n}:a]atrim=start={m.get('offset', 0):.3f},asetpts=PTS-STARTPTS,{tempo}aresample={AR},"
                     f"aformat=channel_layouts=stereo,atrim=0:{total:.3f},volume='{gain}':eval=frame,"
                     f"afade=t=out:st={total - m.get('fade_out', 1.5):.3f}:d={m.get('fade_out', 1.5):.3f}[mus0]")
        if m.get("duck", True):          # dip under the voice; off = a steady bed, as in the reference
            parts.append("[nar]asplit[nar1][key]")
            mixes = ["[nar1]"]
            parts.append(f"[mus0][key]sidechaincompress=threshold={m.get('duck_threshold', 0.03)}:"
                         f"ratio={m.get('duck_ratio', 5)}:"
                         f"attack=15:release={m.get('duck_release', 350)}:makeup=1[mus]")
            mixes.append("[mus]")
        else:
            mixes.append("[mus0]")
        n += 1
    for i, s in enumerate(tl.shots):
        if s.audio_db is None:
            continue
        src = (base / s.src).resolve()
        if not _probe(src)["audio"]:
            continue
        end = tl.shots[i + 1].t if i + 1 < len(tl.shots) else tl.end
        speed = effective_speed(s, end - s.t)
        dur = min(end - s.t, s.audio_len) if s.audio_len else end - s.t
        inputs += ["-ss", f"{s.src_in:.3f}", "-t", f"{dur * speed + 0.3:.3f}", "-i", str(src)]
        tempo = "" if abs(speed - 1) < 1e-3 else f"atempo={max(0.5, speed):.3f},"
        fade = min(0.12, dur / 4)
        parts.append(f"[{n}:a]{tempo}aresample={AR},aformat=channel_layouts=stereo,atrim=0:{dur:.3f},"
                     f"afade=t=in:d={fade:.3f},afade=t=out:st={dur - fade:.3f}:d={fade:.3f},"
                     f"volume={s.audio_db}dB,adelay={int(s.t * 1000)}|{int(s.t * 1000)}[s{i}]")
        mixes.append(f"[s{i}]")
        n += 1
    if tl.sfx:
        sfx = tdir / "sfx.wav"
        _write_wav(sfx, synth_sfx(tl.sfx, total))
        inputs += ["-i", str(sfx)]
        parts.append(f"[{n}:a]atrim=0:{total:.3f}[fx]")
        mixes.append("[fx]")
        n += 1
    parts.append(f"{''.join(mixes)}amix=inputs={len(mixes)}:normalize=0:duration=first,atrim=0:{total:.3f}[mix]")
    raw = tdir / "mix-raw.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(parts), "-map", "[mix]",
                    "-ar", str(AR), str(raw)], check=True)
    # loudness to -14 LUFS with a true-peak ceiling
    meas = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(raw), "-af", "ebur128=peak=true", "-f", "null", "-"],
                          capture_output=True, text=True).stderr
    lufs = float(meas.rsplit("I:", 1)[1].split("LUFS")[0])
    out = tdir / "mix.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af",
                    f"volume={-14.0 - lufs:.2f}dB,alimiter=limit=0.84:level=false", "-ar", str(AR), str(out)],
                   check=True)
    return out


# --- captions and assembly --------------------------------------------------------------------------

def caption_words(words: list[dict[str, Any]], hold: float = 0.7) -> list[Word]:
    """One word at a time; a word stays up until the next one (or `hold` into a pause)."""
    out = []
    for i, w in enumerate(words):
        nxt = words[i + 1]["s"] if i + 1 < len(words) else w["e"] + hold
        e = min(nxt, max(w["e"], w["s"] + 0.12) + hold)
        out.append(Word(str(w["w"]), float(w["s"]), float(e)))
    return out


def render(tl: Timeline, out: Path, work: Path | None = None) -> dict[str, Any]:
    base = Path(tl.base)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".story-", dir=out.parent) as td:
        tdir = Path(work or td)
        tdir.mkdir(parents=True, exist_ok=True)
        starts = [round(s.t * FPS) for s in tl.shots] + [round(tl.end * FPS)]
        report = []
        clips = []
        for i, s in enumerate(tl.shots):
            frames = starts[i + 1] - starts[i]
            if frames <= 0:
                raise ValueError(f"shot {i} ({s.label}) has no frames")
            p = (tdir / f"shot-{i:03d}.mp4").resolve()
            key = json.dumps([s.__dict__, frames, str(base)], sort_keys=True, default=str)
            meta = p.with_suffix(".json")
            if p.exists() and meta.exists() and json.loads(meta.read_text()).get("key") == key:
                info = json.loads(meta.read_text())["info"]           # unchanged shot: reuse it
            else:
                info = render_shot(s, frames / FPS, base, p, frames)
                meta.write_text(json.dumps({"key": key, "info": info}))
            clips.append(p)
            report.append({"i": i, "label": s.label, "src": s.src, "src_in": s.src_in, "t": s.t,
                           "dur": round(frames / FPS, 3), "speed": s.speed, "mode": s.mode, **info})
        black_frames = round(tl.black * FPS)
        if black_frames:
            p = (tdir / "black.mp4").resolve()
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}",
                            "-frames:v", str(black_frames), "-c:v", "libx264", "-preset", "medium", "-crf", "16",
                            "-g", str(FPS), "-pix_fmt", "yuv420p", str(p)], check=True)
            clips.append(p)
        lst = tdir / "list.txt"
        lst.write_text("".join(f"file '{c}'\n" for c in clips))
        video = tdir / "video.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                        str(video)],
                       check=True)
        total = (starts[-1] + black_frames) / FPS
        audio = mix_audio(tl, base, total, tdir)
        # captions: rawvideo band piped over the video
        words = caption_words(tl.words)
        rend = cap.CaptionRenderer(words, "story", W, H, {"top": 0.08, "bottom": 0.18, "left": 0.06, "right": 0.06},
                                   position=tl.caption_position, keywords=tl.keywords,
                                   colors={"keyword": tl.keyword_color})
        proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-i", str(video),
                                 "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{rend.band_h}", "-r", str(FPS),
                                 "-i", "pipe:0",
                                 "-i", str(audio),
                                 "-filter_complex", f"[0:v][1:v]overlay=0:{rend.band_y}:shortest=1[v]",
                                 "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "slow", "-crf", "19",
                                 "-maxrate", "12M", "-bufsize", "24M",
                                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                                 "-t", f"{total:.3f}", str(out)], stdin=subprocess.PIPE)
        assert proc.stdin is not None
        rend.stream(total, FPS, proc.stdin.write)
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("caption pass failed")
    durs = [r["dur"] for r in report]
    return {"out": str(out), "duration": round(total, 3), "shots": len(report),
            "mean_shot": round(float(np.mean(durs)), 2),
            "median_shot": round(float(np.median(durs)), 2), "shot_report": report,
            "cuts_per_10s": [sum(1 for s in tl.shots[1:] if a <= s.t < a + 10) for a in range(0, math.ceil(total), 10)]}
