"""Narration: one synthetic line per script line, assembled with scripted pauses.

Each line is generated separately (so one mispronounced line can be regenerated alone), trimmed to
its speech, then placed with the pause that follows it (`hold` for moments where the pictures and
the original sound should breathe). Word timings come from transcribing each line; the caption
text is always the script's own words, so spellings like "Trae" are never the recogniser's.
"""

from __future__ import annotations

import difflib
import json
import re
import subprocess
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import voicebox

SR = 24000


@dataclass
class Line:
    text: str
    pause: float = 0.45           # silence after the line
    beat: str = ""                # story beat label (for the edit)
    say: str | None = None        # what the voice reads when it differs (e.g. "Tray" for "Trae")


@dataclass
class TimedWord:
    w: str
    s: float
    e: float


@dataclass
class TimedLine:
    text: str
    beat: str
    start: float
    end: float
    pause: float
    words: list[TimedWord] = field(default_factory=list)


def _read(path: Path) -> np.ndarray:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768


def trim(x: np.ndarray, floor_db: float = -45.0, margin: float = 0.03) -> np.ndarray:
    """Drop leading/trailing silence (TTS pads both ends), keeping a short margin."""
    hop = int(0.01 * SR)
    rms = np.array([np.sqrt(np.mean(x[i:i + hop] ** 2)) + 1e-9 for i in range(0, len(x), hop)])
    loud = np.nonzero(20 * np.log10(rms) > floor_db)[0]
    if not len(loud):
        return x
    a = max(0, loud[0] * hop - int(margin * SR))
    b = min(len(x), (loud[-1] + 1) * hop + int(margin * SR))
    return x[a:b]


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text.replace("...", " ").strip()) if re.search(r"\w", t)]


def _align(script_words: list[str], heard: list[tuple[str, float, float]], dur: float) -> list[TimedWord]:
    """Script words with times from the recogniser's words (matched by spelling), gaps interpolated."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    a, b = [norm(w) for w in script_words], [norm(w) for w, _, _ in heard]
    times: list[tuple[float, float] | None] = [None] * len(script_words)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                times[i1 + k] = heard[j1 + k][1:]
        elif j2 > j1:     # replaced (e.g. "twenty-two" heard as "22"): spread the heard span evenly
            s0, e0 = heard[j1][1], heard[j2 - 1][2]
            n = i2 - i1
            for k in range(n):
                times[i1 + k] = (s0 + (e0 - s0) * k / n, s0 + (e0 - s0) * (k + 1) / n)
    # anything still unknown: between its neighbours
    for i, t in enumerate(times):
        if t is None:
            prev = next((tj[1] for tj in reversed(times[:i]) if tj is not None), 0.0)
            nxt = next((tj[0] for tj in times[i + 1:] if tj is not None), dur)
            times[i] = (prev, max(prev + 0.05, nxt))
    return [TimedWord(w, round(s, 3), round(e, 3)) for w, (s, e) in zip(script_words, times)]  # type: ignore[misc]


def build(lines: list[Line], out_dir: Path, voice: str = "bf_emma", lead_in: float = 0.15,
          tail: float = 0.0, cache_dir: Path | None = None) -> tuple[Path, list[TimedLine]]:
    """Generate, trim and assemble; returns the narration WAV and each line's timing."""
    from faster_whisper import WhisperModel

    out_dir.mkdir(parents=True, exist_ok=True)
    asr = WhisperModel("small", device="cpu", compute_type="int8")
    pieces: list[np.ndarray] = [np.zeros(int(lead_in * SR), np.float32)]
    t = lead_in
    timed: list[TimedLine] = []
    for i, ln in enumerate(lines):
        raw = voicebox.speak(ln.say or ln.text, out_dir / f"line-{i:02d}.wav", voice=voice,
                             cache_dir=cache_dir or out_dir / ".cache")
        x = trim(_read(raw))
        seg_path = out_dir / f"line-{i:02d}.trim.wav"
        _write(seg_path, x)
        segs, _ = asr.transcribe(str(seg_path), word_timestamps=True, beam_size=5, language="en")
        heard = [(w.word.strip(), w.start, w.end) for s in segs for w in s.words]
        if ln.say:        # recognised words follow the spoken text; map them back by position
            heard = [(t, a, b) for t, (_, a, b) in zip(_tokens(ln.text), heard)] + heard[len(_tokens(ln.text)):]
        dur = len(x) / SR
        words = [TimedWord(w.w, round(t + w.s, 3), round(t + w.e, 3))
                 for w in _align(_tokens(ln.text), heard, dur)]
        timed.append(TimedLine(ln.text, ln.beat, round(t, 3), round(t + dur, 3), ln.pause, words))
        pieces += [x, np.zeros(int(ln.pause * SR), np.float32)]
        t += dur + ln.pause
    pieces.append(np.zeros(int(tail * SR), np.float32))
    final = out_dir / "final.wav"
    _write(final, np.concatenate(pieces))
    (out_dir / "timing.json").write_text(json.dumps([asdict(t) for t in timed], indent=1))
    return final, timed


def load_timing(path: Path) -> list[TimedLine]:
    out = []
    for d in json.loads(path.read_text()):
        d["words"] = [TimedWord(**w) for w in d["words"]]
        out.append(TimedLine(**d))
    return out


def _write(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
