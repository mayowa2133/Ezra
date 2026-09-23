"""Synthetic, legally clean benchmark footage with ground truth.

Nothing here is committed media except assets/portrait.jpg (public domain, NASA;
see assets/README.md). Speech is synthesized at build time with the local TTS
engine (macOS `say`, or espeak-ng on Linux/Docker); generated audio is not
checked in because macOS voices are licensed for personal use only.

Fixtures
  solo      one speaker, one static talking-head shot
  podcast   host + guest in a wide two-shot (faces left and right)
  multi     three speakers, three faces
  scenes    host + guest; the edit cuts between a wide shot and close-ups of
            whoever is speaking, plus a slide-style cutaway (multi-scene)

Each build writes <name>.mp4 and <name>.truth.json with speaker turns, scene
cuts and face positions, which the benchmark scores against.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageOps

ASSETS = Path(__file__).parent / "assets"
W, H = 1280, 720
FPS = 25

# (speaker, line). Strong moments, filler words, dull stretches and questions.
PODCAST_SCRIPT: list[tuple[str, str]] = [
    ("host", "Welcome back to the show. Today my guest built a company, lost almost everything, and came back."),
    ("host", "So, um, where does the story actually start?"),
    ("guest", "In twenty nineteen I lost four hundred thousand dollars overnight. We wired our entire payroll reserve to a vendor that turned out to be a fraud."),
    ("guest", "I found out at two in the morning and sat on the kitchen floor wondering how I would tell thirty people they might not get paid."),
    ("host", "What did you do next?"),
    ("guest", "Uh, here is what nobody tells founders. The money was not the worst part. The worst part was realizing I skipped every check because I was too proud to ask for help."),
    ("guest", "One phone call to our bank would have stopped it. One call."),
    ("host", "Let us talk about hiring for a minute. How do you think about it?"),
    ("guest", "Hiring is important. You want good people. Culture matters, and you should write down your values early and revisit them every year or so."),
    ("host", "Okay. And, um, what about tools? What does the team use?"),
    ("guest", "We use a lot of spreadsheets. Spreadsheets are fine. We also use a project tracker, and email, and a chat app."),
    ("host", "Was there a moment you almost quit?"),
    ("guest", "I almost quit the company in twenty twenty one. I had my resignation letter written. My co-founder read it, tore it in half, and said give me ninety days."),
    ("guest", "In those ninety days we found the customer that now makes up forty percent of our revenue."),
    ("host", "That is wild. So what would you tell someone who is thinking about quitting right now?"),
    ("guest", "Do not decide at two in the morning. Decide after you have slept, eaten, and talked to the one person who believes in the company more than you do."),
    ("host", "Here is a question I ask everyone. What is the biggest mistake founders make with money?"),
    ("guest", "They raise too early. We raised two million dollars before we had a single paying customer, and it made us lazy. We hired fast and learned slow."),
    ("guest", "The most expensive advice I ever got was, um, grow at all costs. It almost killed us."),
    ("host", "What is something you believe that most people in your industry disagree with?"),
    ("guest", "I think most startups should never raise venture money. Honestly, the best companies I know are profitable, boring, and owned by the people who run them."),
    ("host", "Controversial. Why do you think people disagree?"),
    ("guest", "Because the stories we celebrate are the billion dollar outcomes. Nobody writes articles about the owner who pays herself well and goes home at five."),
    ("host", "Let us do a quick lightning round. Morning routine?"),
    ("guest", "Coffee, a walk, and no email until ten."),
    ("host", "Best book you read this year?"),
    ("guest", "A book about the history of shipping containers. It is surprisingly exciting."),
    ("host", "Last question. If you started over tomorrow with zero dollars, what would you do first?"),
    ("guest", "I would sell something before I built anything. Find ten people who will pay you, then build exactly what they paid for."),
    ("guest", "That one rule would have saved me three years and about a million dollars."),
    ("host", "That is a great place to end. Thanks for coming on, and thanks everyone for listening."),
]

MULTI_EXTRA: list[tuple[str, str]] = [
    ("third", "Can I jump in here? I actually disagree completely. Venture money built the company I work at, and it changed my life."),
    ("guest", "That is fair. It works for some companies. My point is that it should be a choice, not a default."),
    ("third", "Okay, but here is the thing nobody admits. Most founders who say they are bootstrapping just could not raise."),
    ("host", "Ouch. Let us leave that one there."),
]

VOICES = {
    "say": {"host": "Samantha", "guest": "Daniel", "third": "Karen"},
    "espeak-ng": {"host": "en-us+f3", "guest": "en-gb+m3", "third": "en-us+m7"},
}
LONG_PAUSES_AFTER = {6, 10}   # line indexes followed by a 2.5s silence (silence-removal test)


@dataclass
class Line:
    speaker: str
    text: str
    start: float = 0.0
    end: float = 0.0


def tts_engine() -> str:
    if shutil.which("say"):
        return "say"
    if shutil.which("espeak-ng"):
        return "espeak-ng"
    raise RuntimeError("no TTS engine: install espeak-ng (Linux) or run on macOS")


def _synth(text: str, speaker: str, out: Path, engine: str) -> float:
    voice = VOICES[engine][speaker]
    raw = out.with_suffix(".aiff" if engine == "say" else ".raw.wav")
    if engine == "say":
        subprocess.run(["say", "-v", voice, "-r", "185", "-o", str(raw), text], check=True)
    else:
        subprocess.run(["espeak-ng", "-v", voice, "-s", "165", "-w", str(raw), text], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-ac", "1", "-ar", "44100", str(out)], check=True)
    raw.unlink(missing_ok=True)
    return _duration(out)


def _duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def _faces() -> dict[str, Image.Image]:
    base = Image.open(ASSETS / "portrait.jpg").convert("RGB")
    return {
        "host": base,
        "guest": ImageEnhance.Color(ImageOps.mirror(base)).enhance(0.35),
        "third": ImageOps.autocontrast(ImageEnhance.Brightness(base).enhance(0.8)).rotate(0, fillcolor=(0, 0, 0)),
    }


def _frame(layout: list[tuple[str, int, int, int]], bg: tuple[int, int, int], faces: dict[str, Image.Image],
           caption: str | None = None) -> Image.Image:
    """layout: (face key, centre x, centre y, size) in canvas pixels."""
    img = Image.new("RGB", (W, H), bg)
    for key, cx, cy, size in layout:
        face = faces[key].resize((size, size))
        img.paste(face, (cx - size // 2, cy - size // 2))
    if caption:
        d = ImageDraw.Draw(img)
        d.rectangle((0, H - 90, W, H), fill=(20, 20, 20))
        d.text((40, H - 70), caption, fill=(240, 240, 240))
    return img


def build(name: str, out_dir: Path, engine: str | None = None) -> tuple[Path, Path]:
    """Build one fixture; returns (video, truth json). Cached by name in out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    video, truth_path = out_dir / f"{name}.mp4", out_dir / f"{name}.truth.json"
    if video.exists() and truth_path.exists():
        return video, truth_path
    engine = engine or tts_engine()
    script = list(PODCAST_SCRIPT)
    if name == "solo":
        script = [("guest", t) for _, t in PODCAST_SCRIPT]
    elif name == "multi":
        script = PODCAST_SCRIPT[:16] + MULTI_EXTRA + PODCAST_SCRIPT[16:]
    elif name not in ("podcast", "scenes"):
        raise ValueError(f"unknown fixture {name!r}")
    faces = _faces()
    with tempfile.TemporaryDirectory(prefix="ezra-fixture-") as tmp:
        tdir = Path(tmp)
        lines: list[Line] = []
        parts: list[Path] = []
        t = 0.6
        lead = tdir / "lead.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                        "-t", "0.6", str(lead)], check=True)
        parts.append(lead)
        for i, (spk, text) in enumerate(script):
            wav = tdir / f"l{i:03d}.wav"
            d = _synth(text, spk, wav, engine)
            lines.append(Line(spk, text, round(t, 3), round(t + d, 3)))
            parts.append(wav)
            t += d
            gap = 2.5 if i in LONG_PAUSES_AFTER else 0.45
            sil = tdir / f"s{i:03d}.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                            "-t", str(gap), str(sil)], check=True)
            parts.append(sil)
            t += gap
        total = t
        concat = tdir / "audio.txt"
        concat.write_text("".join(f"file '{p}'\n" for p in parts))
        audio = tdir / "audio.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                        "-c:a", "pcm_s16le", str(audio)], check=True)
        total = _duration(audio)

        # --- picture: a list of (start, end, image, face truth) shots -------------
        shots: list[tuple[float, float, Image.Image, list[dict]]] = []
        bg = (38, 44, 58)
        if name == "solo":
            lay = [("guest", 470, 360, 360)]
            shots.append((0.0, total, _frame(lay, bg, faces), _truth(lay)))
        elif name == "podcast":
            lay = [("host", 320, 360, 300), ("guest", 960, 360, 300)]
            shots.append((0.0, total, _frame(lay, bg, faces), _truth(lay)))
        elif name == "multi":
            lay = [("host", 230, 360, 260), ("guest", 640, 360, 260), ("third", 1050, 360, 260)]
            shots.append((0.0, total, _frame(lay, bg, faces), _truth(lay)))
        else:  # scenes: cut per speaker turn between wide and close-ups, plus a cutaway
            wide = [("host", 320, 360, 300), ("guest", 960, 360, 300)]
            close = {"host": [("host", 520, 360, 520)], "guest": [("guest", 760, 360, 520)]}
            cursor = 0.0
            for i, ln in enumerate(lines):
                end = lines[i + 1].start if i + 1 < len(lines) else total
                if i % 7 == 3:
                    img = Image.new("RGB", (W, H), (180, 90, 30))
                    ImageDraw.Draw(img).text((80, 320), "KEY IDEA: SELL BEFORE YOU BUILD", fill=(255, 255, 255))
                    shots.append((cursor, end, img, []))
                elif i % 3 == 0:
                    shots.append((cursor, end, _frame(wide, bg, faces), _truth(wide)))
                else:
                    lay = close[ln.speaker]
                    shots.append((cursor, end, _frame(lay, (58, 40, 44) if ln.speaker == "host" else (40, 58, 50),
                                                      faces), _truth(lay)))
                cursor = end
            merged: list[tuple[float, float, Image.Image, list[dict]]] = []
            for sh in shots:  # merge consecutive identical shots so truth cuts are real cuts
                if merged and merged[-1][3] == sh[3] and merged[-1][2].tobytes() == sh[2].tobytes():
                    merged[-1] = (merged[-1][0], sh[1], merged[-1][2], merged[-1][3])
                else:
                    merged.append(sh)
            shots = merged

        segs = []
        for i, (a, b, img, _) in enumerate(shots):
            png = tdir / f"shot{i:03d}.png"
            img.save(png)
            seg = tdir / f"shot{i:03d}.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(png),
                            "-t", f"{b - a:.3f}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                            "-r", str(FPS), str(seg)], check=True)
            segs.append(seg)
        vlist = tdir / "video.txt"
        vlist.write_text("".join(f"file '{p}'\n" for p in segs))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(vlist),
                        "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest",
                        "-movflags", "+faststart", str(video)], check=True)
    truth = {
        "name": name, "engine": engine, "duration": round(total, 3), "width": W, "height": H,
        "speakers": sorted({ln.speaker for ln in lines}),
        "turns": [{"speaker": ln.speaker, "start": ln.start, "end": ln.end, "text": ln.text} for ln in lines],
        "scene_cuts": [round(a, 3) for a, _, _, _ in shots[1:]],
        "shots": [{"start": round(a, 3), "end": round(b, 3), "faces": f} for a, b, _, f in shots],
        "long_pauses": [round(lines[i].end, 3) for i in sorted(LONG_PAUSES_AFTER) if i < len(lines)],
        "filler_words": ["um", "uh"],
    }
    truth_path.write_text(json.dumps(truth, indent=2))
    return video, truth_path


def _truth(layout: list[tuple[str, int, int, int]]) -> list[dict]:
    return [{"who": k, "cx": round(cx / W, 4), "cy": round(cy / H, 4), "size": round(size / W, 4)}
            for k, cx, cy, size in layout]


def build_all(out_dir: Path, names: tuple[str, ...] = ("solo", "podcast", "multi", "scenes")) -> dict[str, tuple[Path, Path]]:
    return {n: build(n, out_dir) for n in names}
