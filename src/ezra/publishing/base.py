"""Publisher contract and shared OAuth plumbing.

Adapters implement official platform APIs (no password botting, no browser
automation). Each one is complete up to the credential boundary: given valid
OAuth client credentials and a connected account it posts for real; without
them it fails with a clear message naming what is missing. Tests drive every
adapter through an httpx.MockTransport.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as pysecrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .. import secrets
from ..jobs import RetryableError


class PublishError(RuntimeError):
    pass


@dataclass
class PostRequest:
    video: Path
    title: str | None
    caption: str
    description: str | None
    hashtags: list[str]
    visibility: str = "public"            # public | private | unlisted
    scheduled_at: datetime | None = None  # platform-side scheduling when supported
    thumbnail: Path | None = None


@dataclass
class PublishResult:
    status: str                           # published | processing | scheduled
    external_id: str | None
    url: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricsResult:
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    impressions: int | None = None
    avg_watch_seconds: float | None = None
    completion_rate: float | None = None
    followers_gained: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class Publisher(ABC):
    name: str                             # provider id: youtube | tiktok | instagram | upload-post | local-export
    platforms: tuple[str, ...]
    supports_scheduling = False
    supports_private = True

    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=httpx.Timeout(600, connect=20))

    @abstractmethod
    def publish(self, account: dict[str, Any], req: PostRequest, platform: str) -> PublishResult: ...

    def refresh_status(self, account: dict[str, Any], external_id: str) -> PublishResult | None:
        return None

    def metrics(self, account: dict[str, Any], external_id: str) -> MetricsResult | None:
        return None

    # --- OAuth (authorization-code + PKCE) ------------------------------------------------
    oauth_authorize_url: str | None = None
    oauth_scopes: tuple[str, ...] = ()
    client_secret_ref: str | None = None

    def authorize_url(self, state: str, redirect_uri: str, code_challenge: str) -> str:
        raise PublishError(f"{self.name} does not use OAuth")

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict[str, Any]:
        raise PublishError(f"{self.name} does not use OAuth")

    def refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        return token

    def account_label(self, token: dict[str, Any]) -> str:
        return token.get("account") or self.name

    # --- helpers ---------------------------------------------------------------------------
    def _check(self, r: httpx.Response, what: str) -> dict[str, Any]:
        if r.status_code in (429, 500, 502, 503, 504):
            raise RetryableError(f"{self.name} {what}: HTTP {r.status_code} {r.text[:300]}")
        if r.status_code >= 400:
            raise PublishError(f"{self.name} {what}: HTTP {r.status_code} {r.text[:500]}")
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {"text": r.text}

    def token(self, account: dict[str, Any]) -> dict[str, Any]:
        """Stored token for the account, refreshed when it is about to expire."""
        ref = account.get("credential_ref")
        if not ref:
            raise PublishError(f"account {account.get('handle')} on {self.name} has no stored credential; "
                               f"connect it first (ezra accounts connect {self.name})")
        tok = secrets.get(ref)
        if not tok:
            raise PublishError(f"credential {ref!r} missing from the secret store; reconnect the account")
        if isinstance(tok, dict) and tok.get("expires_at") and tok["expires_at"] - time.time() < 120:
            tok = self.refresh(tok)
            secrets.put(ref, tok)
        return tok if isinstance(tok, dict) else {"access_token": tok}

    def client_credentials(self) -> dict[str, Any]:
        assert self.client_secret_ref
        cred = secrets.require(self.client_secret_ref, f"{self.name} OAuth app credentials")
        if isinstance(cred, dict) and len(cred) == 1 and set(cred) <= {"web", "installed"}:
            cred = next(iter(cred.values()))   # Google's downloaded client_secret.json
        if not isinstance(cred, dict) or not cred.get("client_id") or not cred.get("client_secret"):
            raise PublishError(f"secret {self.client_secret_ref!r} must be JSON with client_id and client_secret")
        return cred


def pkce_pair() -> tuple[str, str]:
    verifier = pysecrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def with_query(url: str, params: dict[str, Any]) -> str:
    return f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"


def expires(token: dict[str, Any]) -> dict[str, Any]:
    if token.get("expires_in") and not token.get("expires_at"):
        token["expires_at"] = time.time() + int(token["expires_in"])
    return token
