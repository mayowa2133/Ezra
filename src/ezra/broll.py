"""B-roll: proposals from the transcript, assets only from sources with rights.

Providers
  library   a folder you own/licensed (EZRA_BROLL_LIBRARY): video files matched
            by filename words and optional <file>.json {"tags": [...], "license": ...}
  pexels    Pexels video search (PEXELS_API_KEY); Pexels License permits free use,
            attribution recorded on the asset
  textcard  generated locally: a kinetic keyword card (no third-party footage)
Nothing is inserted automatically without a rights basis; every asset carries
its license string.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from . import candidates, render, secrets
from .analysis.text import NUMBER, tokens
from .config import get_settings
from .render import edl
from .render.spec import BrollInsert
from .storage import get_storage, sha256_file
from .transcription import load_words
from .transcription.base import ends_sentence

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


@dataclass
class BrollAsset:
    provider: str
    id: str
    title: str
    license: str
    attribution: str | None = None
    path: str | None = None
    url: str | None = None
    duration: float | None = None


class BrollProvider(ABC):
    name: str

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[BrollAsset]: ...

    @abstractmethod
    def fetch(self, asset: BrollAsset) -> str:
        """Store the asset; returns its storage key."""


class LibraryProvider(BrollProvider):
    name = "library"

    def __init__(self, root: Path | None = None):
        self.root = root or get_settings().broll_library
        if not self.root or not Path(self.root).is_dir():
            raise RuntimeError("B-roll library not configured: set EZRA_BROLL_LIBRARY to a folder of licensed clips")

    def search(self, query: str, limit: int = 5) -> list[BrollAsset]:
        want = set(tokens(query))
        scored = []
        for p in sorted(Path(self.root).rglob("*")):  # type: ignore[arg-type]
            if p.suffix.lower() not in VIDEO_EXT:
                continue
            side = p.with_suffix(p.suffix + ".json")
            meta = json.loads(side.read_text()) if side.exists() else {}
            tags = set(tokens(p.stem.replace("_", " ").replace("-", " "))) | {t.lower() for t in meta.get("tags", [])}
            overlap = len(want & tags)
            if overlap:
                scored.append((overlap, BrollAsset("library", p.name, p.stem, meta.get("license", "user-provided"),
                                                   meta.get("attribution"), path=str(p))))
        return [a for _, a in sorted(scored, key=lambda x: -x[0])[:limit]]

    def fetch(self, asset: BrollAsset) -> str:
        p = Path(asset.path or "")
        return get_storage().put_file(f"broll/library/{sha256_file(p)[:16]}{p.suffix.lower()}", p)


class PexelsProvider(BrollProvider):
    name = "pexels"
    api = "https://api.pexels.com/videos/search"

    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=60)

    def search(self, query: str, limit: int = 5) -> list[BrollAsset]:
        key = secrets.require("pexels", "Pexels B-roll search")
        r = self.http.get(self.api, params={"query": query, "per_page": limit, "orientation": "portrait"},
                          headers={"Authorization": key})
        if r.status_code >= 400:
            raise RuntimeError(f"Pexels search failed: HTTP {r.status_code}")
        out = []
        for v in r.json().get("videos", []):
            files = sorted((f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"),
                           key=lambda f: abs((f.get("height") or 0) - 1920))
            if files:
                out.append(BrollAsset("pexels", str(v["id"]), v.get("url", ""), "Pexels License (free to use)",
                                      f"Video by {v.get('user', {}).get('name', 'a Pexels creator')} on Pexels",
                                      url=files[0]["link"], duration=v.get("duration")))
        return out

    def fetch(self, asset: BrollAsset) -> str:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            with self.http.stream("GET", asset.url or "") as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"Pexels download failed: HTTP {r.status_code}")
                for chunk in r.iter_bytes():
                    fh.write(chunk)
            tmp = Path(fh.name)
        key = get_storage().put_file(f"broll/pexels/{asset.id}.mp4", tmp)
        get_storage().put_bytes(f"broll/pexels/{asset.id}.json", json.dumps(asdict(asset)).encode())
        tmp.unlink(missing_ok=True)
        return key


class TextCardProvider(BrollProvider):
    """A keyword card rendered locally with ffmpeg + Pillow; rights-free."""
    name = "textcard"

    def search(self, query: str, limit: int = 1) -> list[BrollAsset]:
        return [BrollAsset("textcard", re.sub(r"[^a-z0-9]+", "-", query.lower())[:40] or "card", query.upper(),
                           "generated by Ezra")]

    def fetch(self, asset: BrollAsset) -> str:
        from PIL import Image, ImageDraw

        from .render.captions import _font, find_font

        W, H = 1080, 1920
        img = Image.new("RGB", (W, H), (18, 18, 24))
        d = ImageDraw.Draw(img)
        font = _font(find_font("heavy"), 110)
        words, lines = asset.title.split(), [""]
        for w in words:
            trial = f"{lines[-1]} {w}".strip()
            if d.textlength(trial, font=font) < W * 0.82 or not lines[-1]:
                lines[-1] = trial
            else:
                lines.append(w)
        # upper third: captions sit on the lower safe zone, or on the seam at mid-frame in split layouts
        for i, ln in enumerate(lines):
            d.text((W / 2, H * 0.3 + (i - (len(lines) - 1) / 2) * 130), ln, font=font, anchor="mm",
                   fill=(255, 212, 0))
        with tempfile.TemporaryDirectory() as tmp:
            png, mp4 = Path(tmp) / "card.png", Path(tmp) / "card.mp4"
            img.save(png)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(png), "-t", "4",
                            "-vf", "zoompan=z='min(zoom+0.0015,1.12)':d=120:s=1080x1920,fps=30", "-c:v", "libx264",
                            "-preset", "veryfast", "-pix_fmt", "yuv420p", str(mp4)], check=True, capture_output=True)
            return get_storage().put_file(f"broll/textcard/{asset.id}.mp4", mp4)


PROVIDERS = {"library": LibraryProvider, "pexels": PexelsProvider, "textcard": TextCardProvider}


def get_provider(name: str) -> BrollProvider:
    if name not in PROVIDERS:
        raise ValueError(f"unknown B-roll provider {name!r}; available: {sorted(PROVIDERS)}")
    return PROVIDERS[name]()


FILLER_WORDS = {"honestly", "really", "actually", "basically", "literally", "probably", "something", "anything",
                "everyone", "everything", "nobody", "because", "about", "there", "their", "which", "would",
                "could", "should", "think", "those", "these", "where", "right"}


def headline(text: str) -> str:
    """A text card's words: a number with its unit ("$2 million", "40% of our revenue" -> "40% OF
    OUR"), else up to three content words in the order spoken."""
    m = re.search(r"\$?\d[\d,.]*%?(?:\s+(?:million|billion|thousand|percent|days|years|months|people))?", text)
    if m:
        return m.group(0).strip(" ,.")
    words = [w for w in re.findall(r"[A-Za-z']+", text) if len(w) > 4 and w.lower() not in FILLER_WORDS]
    return " ".join(list(dict.fromkeys(words))[:3]) or text.split()[0]


def propose(candidate_id: int, max_inserts: int = 2, spec: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Where a cutaway helps: sentences with a concrete noun/number, not the
    first 3 seconds (the hook needs the speaker's face). Times are on the
    output (edited) timeline so they drop straight into a RenderSpec."""
    cand = candidates.get(candidate_id)
    words = [w for w in load_words(cand.source_id) if w.e > cand.start and w.s < cand.end]
    sp = spec or {}
    pieces, _ = edl.build(words, cand.start, cand.end, sp.get("remove_silence", True),
                          sp.get("silence_threshold", 0.6), sp.get("remove_fillers", True),
                          render.quiet_regions(cand.source_id))
    out_words = edl.remap_words(words, pieces)
    sentences: list[list[Any]] = [[]]
    for w in out_words:
        sentences[-1].append(w)
        if ends_sentence(w.w):
            sentences.append([])
    proposals = []
    for sent in sentences:
        if not sent or sent[0].s < 3.0:
            continue
        text = " ".join(w.w for w in sent)
        concrete = [t for t in tokens(text) if len(t) > 4] + [n[0] for n in NUMBER.findall(text)]
        if not concrete:
            continue
        query = " ".join(dict.fromkeys(t for t in tokens(text) if len(t) > 4))[:60] or concrete[0]
        span = sent[-1].e - sent[0].s
        proposals.append({"at": round(sent[0].s + 0.2, 2), "duration": round(min(3.0, max(1.5, span)), 2),
                          "query": query, "headline": headline(text), "reason": f"illustrates: {text[:80]}",
                          "score": len(concrete)})
    proposals.sort(key=lambda p: -p["score"])
    chosen: list[dict[str, Any]] = []
    for p in proposals:
        if all(abs(p["at"] - c["at"]) > 6 for c in chosen):
            chosen.append(p)
        if len(chosen) >= max_inserts:
            break
    return sorted(chosen, key=lambda p: p["at"])


def attach(clip_id: int, provider: str = "textcard", max_inserts: int = 2) -> Any:
    """Propose, fetch rights-cleared assets, and re-render the clip with them."""
    clip = render.get_clip(clip_id)
    prov = get_provider(provider)
    current = render.current_version(clip)
    inserts = []
    for p in propose(clip.candidate_id, max_inserts, current.spec if current else None):
        # generated cards show a headline; stock/library search uses the keyword query
        found = prov.search(p["headline"] if provider == "textcard" else p["query"], limit=1)
        if not found:
            continue
        key = prov.fetch(found[0])
        inserts.append(BrollInsert(at=p["at"], duration=p["duration"], asset_key=key, query=p["query"],
                                   reason=f"{p['reason']} [{found[0].license}]").model_dump())
    if not inserts:
        raise RuntimeError("no B-roll matched; add licensed clips to the library or try another provider")
    return render.rerender(clip_id, {"broll": inserts})
