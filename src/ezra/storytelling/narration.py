"""Narration: synthetic script lines, assembled with scripted pauses.

A line is generated on its own (so one mispronounced line can be regenerated alone), unless it shares
a `take` with its neighbours: consecutive lines of one take are read in a single generation, so the
voice carries its intonation from line to line, then split back into lines on the word timings.
Each line is trimmed to its speech and placed with the pause that follows it (`hold` for moments
where the pictures and the original sound should breathe). Word timings come from transcribing each line; the caption
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
    speed: float = 1.0            # delivery pace (a take uses its first line's)
    take: str = ""                # consecutive lines with the same take are read in one generation


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


HOP = int(0.01 * SR)


def _rms(x: np.ndarray) -> np.ndarray:
    return np.array([np.sqrt(np.mean(x[i:i + HOP] ** 2)) + 1e-9 for i in range(0, len(x), HOP)])


def _bounds(x: np.ndarray, floor_db: float = -45.0, margin: float = 0.03) -> tuple[int, int]:
    """Sample range of the speech in `x`, with a short margin."""
    loud = np.nonzero(20 * np.log10(_rms(x)) > floor_db)[0]
    if not len(loud):
        return 0, len(x)
    return max(0, loud[0] * HOP - int(margin * SR)), min(len(x), (loud[-1] + 1) * HOP + int(margin * SR))


def trim(x: np.ndarray, floor_db: float = -45.0, margin: float = 0.03) -> np.ndarray:
    """Drop leading/trailing silence (TTS pads both ends), keeping a short margin."""
    a, b = _bounds(x, floor_db, margin)
    return x[a:b]


def split_take(x: np.ndarray, words: list[list[TimedWord]]) -> list[tuple[np.ndarray, list[TimedWord], float]]:
    """Cut one take back into its lines: each cut is at the quietest 10 ms between a line's last word
    and the next line's first. Returns each line's trimmed audio, its words (re-based to that audio)
    and where the audio starts in the take (seconds)."""
    cuts = [0]
    for prev, nxt in zip(words, words[1:]):
        a, b = int(prev[-1].e * SR), int(nxt[0].s * SR)
        if b - a > HOP:
            a += int(np.argmin(_rms(x[a:b])[:(b - a) // HOP])) * HOP + HOP // 2
        else:
            a = (a + b) // 2
        cuts.append(max(cuts[-1], min(len(x), a)))
    cuts.append(len(x))
    out = []
    for (c0, c1), ws in zip(zip(cuts, cuts[1:]), words):
        a, b = _bounds(x[c0:c1])
        t0 = (c0 + a) / SR
        out.append((x[c0 + a:c0 + b], [TimedWord(w.w, round(max(0.0, w.s - t0), 3), round(max(0.0, w.e - t0), 3))
                                       for w in ws], t0))
    return out


def _sentence(text: str) -> str:
    return text if text.rstrip().endswith((".", "!", "?", "...")) else text.rstrip() + "."


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


def _groups(lines: list[Line]) -> list[list[int]]:
    """Line indices read together: a run of one non-empty take, or a single line."""
    out: list[list[int]] = []
    for i, ln in enumerate(lines):
        if ln.take and out and lines[out[-1][-1]].take == ln.take:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def build(lines: list[Line], out_dir: Path, voice: str = "bf_emma", lead_in: float = 0.15,
          tail: float = 0.0, cache_dir: Path | None = None) -> tuple[Path, list[TimedLine]]:
    """Generate, trim and assemble; returns the narration WAV and each line's timing."""
    from faster_whisper import WhisperModel

    out_dir.mkdir(parents=True, exist_ok=True)
    asr = WhisperModel("small", device="cpu", compute_type="int8")
    # each line's audio, its words (relative to that audio) and the natural gap after it inside its take
    parts: list[tuple[np.ndarray, list[TimedWord], float]] = []
    for group in _groups(lines):
        lns = [lines[i] for i in group]
        name = f"take-{lns[0].take}" if lns[0].take else f"line-{group[0]:02d}"
        raw = voicebox.speak(" ".join(_sentence(ln.say or ln.text) for ln in lns), out_dir / f"{name}.wav",
                             voice=voice, speed=lns[0].speed, cache_dir=cache_dir or out_dir / ".cache")
        x = trim(_read(raw))
        seg_path = out_dir / f"{name}.trim.wav"
        _write(seg_path, x)
        segs, _ = asr.transcribe(str(seg_path), word_timestamps=True, beam_size=5, language="en")
        heard = [(w.word.strip(), w.start, w.end) for s in segs for w in s.words]
        spoken = [_tokens(ln.say or ln.text) for ln in lns]
        timed_spoken = _align([w for ws in spoken for w in ws], heard, len(x) / SR)
        words: list[list[TimedWord]] = []
        for ln, sp in zip(lns, spoken):
            mine, timed_spoken = timed_spoken[:len(sp)], timed_spoken[len(sp):]
            # the caption keeps the script's spelling: map `say` words back ("one oh four" -> "one-oh-four")
            words.append(_align(_tokens(ln.text), [(w.w, w.s, w.e) for w in mine], mine[-1].e))
        split = split_take(x, words) if len(lns) > 1 else [(x, words[0], 0.0)]
        for k, (seg, ws, t0) in enumerate(split):
            gap = split[k + 1][2] - (t0 + len(seg) / SR) if k + 1 < len(split) else 0.0
            parts.append((seg, ws, max(0.0, gap)))
    pieces: list[np.ndarray] = [np.zeros(int(lead_in * SR), np.float32)]
    t = lead_in
    timed: list[TimedLine] = []
    for ln, (x, ws, gap) in zip(lines, parts):
        dur, pause = len(x) / SR, max(ln.pause, gap)        # a longer natural gap inside a take is kept
        timed.append(TimedLine(ln.text, ln.beat, round(t, 3), round(t + dur, 3), round(pause, 3),
                               [TimedWord(w.w, round(t + w.s, 3), round(t + w.e, 3)) for w in ws]))
        pieces += [x, np.zeros(int(pause * SR), np.float32)]
        t += dur + pause
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
