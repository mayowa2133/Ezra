"""Narration through a local Voicebox server (https://github.com/jamiepine/voicebox).

Voicebox runs on the user's machine (default http://127.0.0.1:17493, `EZRA_VOICEBOX_URL` to change it)
and exposes preset voices, e.g. Kokoro's British `bf_alice`. Nothing is sent to a paid API.
Generated audio is cached by (profile, engine, text), so re-running a timeline only regenerates the
lines whose text changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:17493"


class VoiceboxError(RuntimeError):
    pass


def _url() -> str:
    return os.environ.get("EZRA_VOICEBOX_URL", DEFAULT_URL).rstrip("/")


def _call(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 60) -> Any:
    req = urllib.request.Request(_url() + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return raw if path.endswith("export-audio") else json.loads(raw or b"null")
    except urllib.error.URLError as e:
        raise VoiceboxError(f"Voicebox isn't reachable at {_url()} ({e}); start the Voicebox app") from e


def health() -> dict[str, Any]:
    return _call("GET", "/health", timeout=5)


def preset_profile(engine: str, voice_id: str, name: str | None = None) -> str:
    """Id of a Voicebox profile for a preset voice, created on first use."""
    for p in _call("GET", "/profiles"):
        if p.get("preset_engine") == engine and p.get("preset_voice_id") == voice_id:
            return str(p["id"])
    created = _call("POST", "/profiles", {"name": name or f"Ezra {voice_id} ({engine})", "language": "en",
                                          "voice_type": "preset", "preset_engine": engine,
                                          "preset_voice_id": voice_id, "default_engine": engine})
    return str(created["id"])


def speak(text: str, dest: Path, voice: str = "bf_alice", engine: str = "kokoro",
          cache_dir: Path | None = None, timeout: float = 300) -> Path:
    """Render `text` to a WAV at `dest` (cached)."""
    key = hashlib.sha256(json.dumps([engine, voice, text]).encode()).hexdigest()[:20]
    cache = (cache_dir or dest.parent) / f".vb-{key}.wav"
    if not cache.exists():
        profile = preset_profile(engine, voice)
        gen = _call("POST", "/generate", {"profile_id": profile, "text": text, "engine": engine, "language": "en"})
        gid, t0 = gen["id"], time.time()
        while True:
            status = _call("GET", f"/history/{gid}")
            if status.get("status") == "completed":
                break
            if status.get("status") == "failed" or status.get("error"):
                raise VoiceboxError(f"generation failed: {status.get('error')}")
            if time.time() - t0 > timeout:
                raise VoiceboxError(f"generation timed out after {timeout:.0f}s")
            time.sleep(0.5)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(_call("GET", f"/history/{gid}/export-audio", timeout=120))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() != cache.resolve():
        dest.write_bytes(cache.read_bytes())
    return dest
