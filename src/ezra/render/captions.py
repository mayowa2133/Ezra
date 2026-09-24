"""Word-level animated captions, driven by data.

A theme is a dict of parameters; one renderer draws every theme. Frames are
drawn with Pillow into a transparent band (only the caption region, not the
whole frame) and streamed to ffmpeg as raw RGBA, then overlaid. This needs no
libass (Homebrew's ffmpeg has none) and supports per-word colour, karaoke fill,
pop/rise/fade animation and keyword highlighting. SRT and styled ASS files are
exported alongside for editors and platforms that accept sidecar captions.

The themes are generic short-form aesthetics, not copies of any product's
proprietary presets or fonts.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..analysis.text import LEXICON, NUMBER
from ..transcription.base import Word, ends_sentence, join_words

HEAVY_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
REGULAR_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

# size is relative to a 1080px-wide frame and scaled to the output width
THEMES: dict[str, dict[str, Any]] = {
    "clean": {"weight": "heavy", "size": 64, "case": "sentence", "fill": "#FFFFFF", "stroke": "#000000",
              "stroke_w": 5, "active": None, "mode": "chunk", "anim": "none", "words": 4, "position": 0.70,
              "box": None, "shadow": False},
    "bold": {"weight": "heavy", "size": 82, "case": "upper", "fill": "#FFFFFF", "stroke": "#000000",
             "stroke_w": 8, "active": "#FFD400", "mode": "chunk", "anim": "pop", "words": 3, "position": 0.68,
             "box": None, "shadow": False},
    "karaoke": {"weight": "heavy", "size": 72, "case": "sentence", "fill": "#FFFFFF", "dim": "#FFFFFF80",
                "stroke": "#000000", "stroke_w": 6, "active": "#39FF14", "mode": "karaoke", "anim": "none",
                "words": 5, "position": 0.70, "box": None, "shadow": False},
    "cinematic": {"weight": "regular", "size": 54, "case": "sentence", "fill": "#F4F1EA", "stroke": None,
                  "stroke_w": 0, "active": None, "mode": "chunk", "anim": "fade", "words": 7, "position": 0.80,
                  "box": None, "shadow": True},
    "minimal": {"weight": "regular", "size": 50, "case": "sentence", "fill": "#FFFFFF", "stroke": None,
                "stroke_w": 0, "active": None, "mode": "chunk", "anim": "none", "words": 6, "position": 0.78,
                "box": "#000000A6", "shadow": False},
    # matched to top MrBeast Shorts: one or two words at a time, sentence case, white with a thin
    # outline, mid-frame near the action, emphasis words (money, numbers, stakes) in colour
    "pop": {"weight": "heavy", "size": 66, "case": "sentence", "fill": "#FFFFFF", "stroke": "#000000",
            "stroke_w": 5, "active": None, "mode": "chunk", "anim": "pop", "words": 2, "position": 0.58,
            "box": None, "shadow": False},
    "high-impact": {"weight": "heavy", "size": 104, "case": "upper", "fill": "#FFFFFF", "stroke": "#000000",
                    "stroke_w": 10, "active": "#FF3B30", "mode": "word", "anim": "pop", "words": 2,
                    "position": 0.62, "box": None, "shadow": False},
}
ANIM_SECONDS = 0.14
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
EMPHASIS = set().union(*LEXICON.values())


def burnable(text: str) -> str:
    """System fonts have no emoji glyphs (they render as boxes)."""
    return " ".join(_EMOJI.sub("", text).split())


def rgba(hex_color: str | None) -> tuple[int, int, int, int] | None:
    if not hex_color:
        return None
    h = hex_color.lstrip("#")
    if len(h) == 6:
        h += "FF"
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4, 6))  # type: ignore[return-value]


def find_font(weight: str, override: str | None = None) -> str | None:
    for p in [override, *(HEAVY_FONTS if weight == "heavy" else REGULAR_FONTS), *HEAVY_FONTS]:
        if p and os.path.exists(p):
            return p
    return None


@lru_cache(maxsize=64)
def _font(path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


@dataclass
class Chunk:
    words: list[Word]
    start: float
    end: float


def chunk_words(words: list[Word], per_chunk: int, max_span: float = 1.6) -> list[Chunk]:
    chunks: list[list[Word]] = []
    for w in words:
        cur = chunks[-1] if chunks else None
        if (cur is None or len(cur) >= per_chunk or w.s - cur[0].s > max_span or w.s - cur[-1].e > 0.6
                or ends_sentence(cur[-1].w)):
            chunks.append([w])
        else:
            cur.append(w)
    out = []
    for i, ch in enumerate(chunks):
        end = ch[-1].e
        if i + 1 < len(chunks) and chunks[i + 1][0].s - end < 0.35:
            end = chunks[i + 1][0].s  # bridge short gaps: no flicker
        out.append(Chunk(ch, ch[0].s, end))
    return out


EMOJI_FONTS = ["/System/Library/Fonts/Apple Color Emoji.ttc",
               "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", "/usr/share/fonts/noto/NotoColorEmoji.ttf"]
# One emoji above a caption when it has a word worth illustrating (the Submagic / Opus caption look).
EMOJI_MAP = {
    "💰": {"money", "cash", "dollars", "dollar", "paid", "pay", "rich", "prize"},
    "🤑": {"million", "millions", "billion", "billions"},
    "🏆": {"win", "wins", "won", "winner", "winning", "champion"},
    "🚓": {"cops", "cop", "police", "officer", "officers"},
    "🚨": {"arrest", "arrested", "caught", "busted", "alarm"},
    "🏃": {"escape", "escaped", "escaping", "run", "running", "ran", "chase", "chased"},
    "🤫": {"secret", "secrets", "hidden", "hiding", "hide", "sneak", "sneaking"},
    "💥": {"explode", "exploded", "explosion", "bomb", "detonator", "crash", "crashed", "boom"},
    "🔥": {"fire", "burning", "hot", "insane", "crazy"},
    "🤯": {"shocked", "unbelievable", "impossible", "mind", "genius"},
    "😱": {"scared", "afraid", "terrified", "scary", "danger", "dangerous"},
    "😂": {"funny", "laugh", "laughing", "hilarious", "haha", "joke"},
    "💀": {"dead", "died", "fail", "failed", "lost", "lose", "worst"},
    "🔒": {"jail", "prison", "locked", "lock", "trapped", "trap", "cell"},
    "⏰": {"minutes", "hours", "seconds", "time", "clock", "deadline"},
    "🧠": {"plan", "strategy", "smart", "idea"},
    "❤️": {"love", "mom", "mother", "family"},
    "📱": {"phone", "camera", "cameras"},
    "🍕": {"food", "pizza", "eat", "eating"},
}
_WORD_EMOJI = {w: e for e, ws in EMOJI_MAP.items() for w in ws}


def emoji_for(words: list[str]) -> str | None:
    for w in words:
        e = _WORD_EMOJI.get(w.lower().strip(" ,.!?;:\"'"))
        if e:
            return e
        if w.strip().startswith("$"):
            return "💰"
    return None


@lru_cache(maxsize=64)
def emoji_image(char: str, px: int) -> Image.Image | None:
    """A colour emoji at px size, from the system's colour emoji font (None when there isn't one)."""
    for path in EMOJI_FONTS:
        if not os.path.exists(path):
            continue
        for native in (160, 137, 109, 96, 64):   # bitmap strike sizes of Apple / Noto colour fonts
            try:
                font = ImageFont.truetype(path, native)
            except OSError:
                continue
            im = Image.new("RGBA", (native * 2, native * 2), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((native // 2, native // 2), char, font=font, embedded_color=True)
            box = im.getbbox()
            if box:
                return im.crop(box).resize((px, px), Image.Resampling.LANCZOS)
    return None


def is_keyword(word: str, extra: set[str]) -> bool:
    w = word.lower().strip(" ,.!?;:\"'")
    return bool(NUMBER.search(word)) or w in EMPHASIS or w in extra


class CaptionRenderer:
    def __init__(self, words: list[Word], theme: str, width: int, height: int, safe: dict[str, float],
                 font: str | None = None, size: int | None = None, position: float | None = None,
                 colors: dict[str, str] | None = None, keywords: list[str] | None = None,
                 emoji: bool = False):
        if theme not in THEMES:
            raise ValueError(f"unknown caption theme {theme!r}; available: {sorted(THEMES)}")
        self.t = dict(THEMES[theme])
        colors = colors or {}
        self.t["fill"] = colors.get("text", self.t["fill"])
        self.t["active"] = colors.get("active", self.t["active"])
        self.keyword_color = rgba(colors.get("keyword") or ("#FFD400" if self.t["active"] != "#FFD400"
                                                            else "#39D0FF"))
        if self.t.get("box"):
            self.t["box"] = colors.get("box", self.t["box"])
        self.W, self.H = width, height
        scale = width / 1080 if height >= width else height / 1080
        self.size = int((size or self.t["size"]) * scale)
        self.font_path = find_font(self.t["weight"], font)
        self.font = _font(self.font_path, self.size)
        self.pad = int(self.size * 0.35)
        self.max_w = int(width * (1 - safe.get("left", 0.05) - safe.get("right", 0.05)))
        self.line_h = int(self.size * 1.2)
        self.band_h = self.line_h * 3 + 2 * self.pad
        centre = position or self.t["position"]
        lo = int(height * safe.get("top", 0.08))
        hi = int(height * (1 - safe.get("bottom", 0.15))) - self.band_h
        self.band_y = max(lo, min(hi, int(height * centre - self.band_h / 2)))
        self.keywords = {k.lower() for k in (keywords or [])}
        per = 1 if self.t["mode"] == "word" and self.t["words"] == 1 else self.t["words"]
        self.chunks = chunk_words(words, per)
        self._cache: dict[tuple, bytes] = {}
        self.emoji = [emoji_for([w.w for w in ch.words]) if emoji else None for ch in self.chunks]

    def _text(self, w: str) -> str:
        w = burnable(w)
        return w.upper() if self.t["case"] == "upper" else w

    def _layout(self, chunk: Chunk, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> list[list[int]]:
        draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        space = draw.textlength(" ", font=font)
        lines: list[list[int]] = [[]]
        width = 0.0
        for i, w in enumerate(chunk.words):
            ww = draw.textlength(self._text(w.w), font=font)
            if lines[-1] and width + space + ww > self.max_w - 2 * self.pad:
                lines.append([])
                width = 0.0
            width += (space if lines[-1] else 0) + ww
            lines[-1].append(i)
        return lines[:3]

    def _state(self, t: float) -> tuple | None:
        for ci, ch in enumerate(self.chunks):
            if ch.start <= t < ch.end:
                active = next((i for i, w in enumerate(ch.words) if w.s <= t < max(w.e, w.s + 0.05)), None)
                if active is None:
                    done = [i for i, w in enumerate(ch.words) if w.e <= t]
                    active = done[-1] if done else 0
                phase = 0
                anim = self.t["anim"]
                since = t - (ch.words[active].s if anim == "pop" else ch.start)
                if anim in ("pop", "fade", "rise") and since < ANIM_SECONDS:
                    phase = 1 + int(since / ANIM_SECONDS * 4)
                return (ci, active, phase)
        return None

    def frame(self, t: float) -> bytes:
        state = self._state(t)
        key = state or ("empty",)
        if key in self._cache:
            return self._cache[key]
        img = Image.new("RGBA", (self.W, self.band_h), (0, 0, 0, 0))
        if state is not None:
            self._draw(img, *state)
        data = img.tobytes()
        if len(self._cache) > 400:
            self._cache.clear()
        self._cache[key] = data
        return data

    def _draw(self, img: Image.Image, ci: int, active: int, phase: int) -> None:
        ch = self.chunks[ci]
        draw = ImageDraw.Draw(img)
        font = self.font
        lines = self._layout(ch, font)
        space = draw.textlength(" ", font=font)
        alpha = 255
        dy = 0
        if self.t["anim"] == "fade" and phase:
            alpha = int(255 * phase / 5)
        if self.t["anim"] == "rise" and phase:
            dy = int((5 - phase) * self.size * 0.05)
        total_h = self.line_h * len(lines)
        y0 = (self.band_h - total_h) // 2 + dy
        mark = self.emoji[ci]
        if mark:
            px = int(self.size * 1.15)
            icon = emoji_image(mark, px) if y0 - px - self.pad // 3 >= 0 else None
            if icon is not None:
                img.alpha_composite(icon, (int((self.W - px) / 2), int(y0 - px - self.pad // 3)))
        fill = rgba(self.t["fill"])
        dim = rgba(self.t.get("dim"))
        act = rgba(self.t["active"])
        stroke = rgba(self.t["stroke"])
        if self.t.get("box"):
            widths = [sum(draw.textlength(self._text(ch.words[i].w), font=font) for i in ln) + space * (len(ln) - 1)
                      for ln in lines]
            bw = max(widths) + 2 * self.pad
            bx = (self.W - bw) / 2
            draw.rounded_rectangle((bx, y0 - self.pad * 0.6, bx + bw, y0 + total_h + self.pad * 0.4),
                                   radius=int(self.size * 0.3), fill=rgba(self.t["box"]))
        for li, ln in enumerate(lines):
            texts = [self._text(ch.words[i].w) for i in ln]
            widths = [draw.textlength(tx, font=font) for tx in texts]
            x = (self.W - (sum(widths) + space * (len(ln) - 1))) / 2
            y = y0 + li * self.line_h
            for i, tx, wdt in zip(ln, texts, widths):
                color = fill
                wf = font
                if self.t["mode"] == "karaoke":
                    color = act if i <= active else (dim or fill)
                elif act is not None and i == active:
                    color = act
                elif is_keyword(ch.words[i].w, self.keywords) and self.keyword_color:
                    color = self.keyword_color
                if self.t["anim"] == "pop" and i == active and phase:
                    wf = _font(self.font_path, int(self.size * (1.0 + 0.16 * (5 - phase) / 4)))
                assert color is not None
                c = (color[0], color[1], color[2], min(color[3], alpha))
                cx = x + wdt / 2
                cy = y + self.line_h / 2
                if self.t.get("shadow"):
                    draw.text((cx + 3, cy + 3), tx, font=wf, anchor="mm", fill=(0, 0, 0, int(alpha * 0.7)))
                draw.text((cx, cy), tx, font=wf, anchor="mm", fill=c,
                          stroke_width=int(self.t["stroke_w"] * self.size / 82) if stroke else 0,
                          stroke_fill=stroke)
                x += wdt + space

    def stream(self, duration: float, fps: int, write: Callable[[bytes], object]) -> int:
        n = int(round(duration * fps))
        for i in range(n):
            write(self.frame(i / fps))
        return n


# --- static overlays ------------------------------------------------------------------

def hook_card(text: str, width: int, font_path: str | None = None) -> Image.Image | None:
    text = burnable(text)
    if not text:
        return None
    size = int(58 * width / 1080)
    font = _font(font_path or find_font("heavy"), size)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    words, lines = text.split(), [""]
    for w in words:
        trial = f"{lines[-1]} {w}".strip()
        if probe.textlength(trial, font=font) <= width * 0.78 or not lines[-1]:
            lines[-1] = trial
        else:
            lines.append(w)
    lh = int(size * 1.25)
    bw = int(max(probe.textlength(ln, font=font) for ln in lines)) + int(size * 1.3)
    bh = lh * len(lines) + int(size * 0.8)
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=int(size * 0.45), fill=(255, 255, 255, 242))
    for i, ln in enumerate(lines):
        d.text((bw / 2, int(size * 0.4) + i * lh + lh / 2), ln, font=font, anchor="mm", fill=(12, 12, 12, 255))
    return img


def text_badge(text: str, width: int, size_px: int, opacity: int = 170) -> Image.Image:
    font = _font(find_font("heavy"), size_px)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw = int(probe.textlength(burnable(text), font=font))
    img = Image.new("RGBA", (tw + size_px, int(size_px * 1.6)), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((size_px // 2, int(size_px * 0.8)), burnable(text), font=font, anchor="lm",
                             fill=(255, 255, 255, opacity), stroke_width=2, stroke_fill=(0, 0, 0, opacity // 2))
    return img


def cta_card(text: str, width: int, color: str | None = None) -> Image.Image:
    size = int(56 * width / 1080)
    font = _font(find_font("heavy"), size)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw = int(probe.textlength(burnable(text), font=font))
    bw, bh = min(int(width * 0.86), tw + 2 * size), int(size * 2.1)
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=int(size * 0.5), fill=rgba(color or "#FFD400"))
    d.text((bw / 2, bh / 2), burnable(text), font=font, anchor="mm", fill=(15, 15, 15, 255))
    return img


# --- sidecar exports -------------------------------------------------------------------

def _srt_ts(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def to_srt(words: list[Word], per_chunk: int = 6) -> str:
    out = []
    for i, ch in enumerate(chunk_words(words, per_chunk, max_span=3.0), 1):
        out.append(f"{i}\n{_srt_ts(ch.start)} --> {_srt_ts(ch.end)}\n{burnable(join_words([w.w for w in ch.words]))}\n")
    return "\n".join(out)


def _ass_color(hex_color: str | None, default: str = "#FFFFFF") -> str:
    r, g, b, a = rgba(hex_color or default) or (255, 255, 255, 255)
    return f"&H{255 - a:02X}{b:02X}{g:02X}{r:02X}"


def to_ass(words: list[Word], theme: str, width: int, height: int) -> str:
    """Styled ASS with \\k karaoke timing, for libass-based players and editors."""
    t = THEMES[theme]
    size = int(t["size"] * (width / 1080 if height >= width else height / 1080))
    margin_v = int(height * (1 - t["position"])) - size
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Arial,{size},{_ass_color(t['active'] or t['fill'])},{_ass_color(t['fill'])},"
            f"{_ass_color(t['stroke'] or '#000000')},&H80000000,{-1 if t['weight'] == 'heavy' else 0},0,0,0,100,100,"
            f"0,0,{3 if t.get('box') else 1},{max(0, t['stroke_w'] // 2)},{2 if t.get('shadow') else 0},2,60,60,"
            f"{max(10, margin_v)},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text\n")
    lines = []
    for ch in chunk_words(words, t["words"]):
        parts = []
        for w in ch.words:
            txt = burnable(w.w)
            txt = txt.upper() if t["case"] == "upper" else txt
            parts.append(f"{{\\k{max(1, int(round((w.e - w.s) * 100)))}}}{txt}")
        lines.append(f"Dialogue: 0,{_ass_ts(ch.start)},{_ass_ts(ch.end)},Default,,0,0,0,,{' '.join(parts)}")
    return head + "\n".join(lines) + "\n"
