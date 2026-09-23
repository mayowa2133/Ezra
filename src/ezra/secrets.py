"""Secret store. Application tables hold only a `secret_ref`; the secret lives
here. Two layers, checked in order:

1. environment: EZRA_SECRET_<REF> (REF upper-cased, non-alphanumerics → "_"),
   plus the conventional provider variables listed in ENV_ALIASES;
2. an encrypted file ($EZRA_HOME/secrets.enc, Fernet/AES-128-CBC+HMAC) keyed by
   EZRA_SECRET_KEY. Writing requires the key; without it the store is read-only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
from typing import Any

from .config import get_settings

ENV_ALIASES = {
    "upload-post": "UPLOAD_POST_API_KEY",
    "youtube-client": "YOUTUBE_CLIENT_SECRET_JSON",
    "tiktok-client": "TIKTOK_CLIENT_SECRET",
    "instagram-client": "INSTAGRAM_APP_SECRET",
    "huggingface": "HF_TOKEN",
    "pexels": "PEXELS_API_KEY",
    "openshorts": "OPENSHORTS_API_KEY",
    "llm": "EZRA_LLM_API_KEY",
}

_lock = threading.Lock()


class SecretError(RuntimeError):
    pass


def _env_name(ref: str) -> str:
    return "EZRA_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", ref).upper()


def _fernet():
    from cryptography.fernet import Fernet

    key = get_settings().secret_key
    if not key:
        raise SecretError("EZRA_SECRET_KEY is not set; the encrypted secret store is unavailable")
    digest = hashlib.sha256(key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _path():
    return get_settings().home / "secrets.enc"


def _load() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(_fernet().decrypt(p.read_bytes()))
    except SecretError:
        return {}
    except Exception as e:
        raise SecretError(f"cannot decrypt {p}: wrong EZRA_SECRET_KEY?") from e


def get(ref: str) -> Any | None:
    for name in (_env_name(ref), ENV_ALIASES.get(ref)):
        if name and os.environ.get(name):
            raw = os.environ[name]
            try:
                return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
            except json.JSONDecodeError:
                return raw
    with _lock:
        return _load().get(ref)


def require(ref: str, what: str) -> Any:
    value = get(ref)
    if value in (None, ""):
        env = ENV_ALIASES.get(ref) or _env_name(ref)
        raise SecretError(f"{what} needs the secret '{ref}': set {env} or store it with "
                          f"`ezra secrets set {ref}` (requires EZRA_SECRET_KEY)")
    return value


def put(ref: str, value: Any) -> None:
    with _lock:
        data = _load()
        data[ref] = value
        p = _path()
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(_fernet().encrypt(json.dumps(data).encode()))
        os.chmod(tmp, 0o600)
        tmp.replace(p)


def delete(ref: str) -> None:
    with _lock:
        data = _load()
        if data.pop(ref, None) is not None:
            _path().write_bytes(_fernet().encrypt(json.dumps(data).encode()))


def refs() -> list[str]:
    """Names only (never values) of secrets available from either layer."""
    names = set()
    with _lock:
        names |= set(_load())
    for ref, env in ENV_ALIASES.items():
        if os.environ.get(env):
            names.add(ref)
    return sorted(names)
