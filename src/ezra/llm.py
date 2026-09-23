"""Model providers for the judgment tasks (candidate reasoning, critique, hooks,
titles, captions, rule extraction, performance narratives).

Ezra is not tied to one model company, and it does not call a hosted model API
by default. Providers:

  heuristic          no model. Every caller has a deterministic fallback, so the
                     whole pipeline runs with this setting (tests, CI, offline).
  claude-cli         Claude Code in headless mode (`claude -p --json-schema`),
                     using the operator's Claude subscription.
  codex-cli          OpenAI Codex CLI (`codex exec --output-schema`).
  openai-compatible  any /v1/chat/completions endpoint (Ollama, LM Studio, vLLM,
                     llama.cpp, OpenRouter). EZRA_LLM_BASE_URL + optional key.

Every call returns validated JSON matching the caller's schema and records usage
in cost accounting.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import costs, secrets
from .config import get_settings

CLI_TIMEOUT = 600   # seconds per structured call; the critic batches to stay well inside it


class LLMUnavailable(RuntimeError):
    """The configured provider cannot run a model call (callers fall back)."""


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str | None
    seconds: float
    usage: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    name: str

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], task: str) -> LLMResult: ...


class HeuristicProvider(LLMProvider):
    name = "heuristic"

    @property
    def available(self) -> bool:
        return False

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], task: str) -> LLMResult:
        raise LLMUnavailable("EZRA_LLM=heuristic: no model configured; using deterministic scoring")


def _validate(data: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """Minimal structural check (type object + required keys). Full JSON Schema
    validation is done by the CLIs themselves; this guards the HTTP path."""
    if not isinstance(data, dict):
        raise LLMError(f"model returned {type(data).__name__}, expected an object")
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise LLMError(f"model output missing required keys {missing}")
    return data


class ClaudeCLIProvider(LLMProvider):
    name = "claude-cli"

    def __init__(self) -> None:
        self.model = get_settings().llm_model

    @property
    def available(self) -> bool:
        return shutil.which("claude") is not None

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], task: str) -> LLMResult:
        if not self.available:
            raise LLMUnavailable("`claude` CLI not found on PATH")
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema),
               "--system-prompt", system, "--tools", "", "--no-session-persistence",
               "--strict-mcp-config", "--disable-slash-commands"]
        if self.model:
            cmd += ["--model", self.model]
        t0 = time.time()
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT, cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude timed out after {CLI_TIMEOUT}s") from e
        if out.returncode != 0:
            raise LLMError(f"claude exited {out.returncode}: {(out.stderr or out.stdout).strip()[-600:]}")
        payload = json.loads(out.stdout)
        if payload.get("is_error"):
            raise LLMError(f"claude error: {payload.get('result') or payload.get('subtype')}")
        data = payload.get("structured_output")
        if data is None:
            raise LLMError("claude returned no structured_output")
        usage = payload.get("usage") or {}
        return LLMResult(_validate(data, schema), self.name, self.model, time.time() - t0,
                         {"input_tokens": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                          + usage.get("cache_read_input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                          "notional_usd": payload.get("total_cost_usd")})


class CodexCLIProvider(LLMProvider):
    name = "codex-cli"

    def __init__(self) -> None:
        self.model = get_settings().llm_model

    @property
    def available(self) -> bool:
        return shutil.which("codex") is not None

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], task: str) -> LLMResult:
        if not self.available:
            raise LLMUnavailable("`codex` CLI not found on PATH")
        with tempfile.TemporaryDirectory(prefix="ezra-codex-") as tmp:
            schema_file = Path(tmp) / "schema.json"
            schema_file.write_text(json.dumps(_strict(schema)))
            last = Path(tmp) / "last.json"
            cmd = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "--output-schema", str(schema_file), "-o", str(last)]
            if self.model:
                cmd += ["--model", self.model]
            t0 = time.time()
            try:
                out = subprocess.run([*cmd, f"{system}\n\n{prompt}"], capture_output=True, text=True,
                                     timeout=CLI_TIMEOUT, cwd=tmp)
            except subprocess.TimeoutExpired as e:
                raise LLMError(f"codex timed out after {CLI_TIMEOUT}s") from e
            if out.returncode != 0 or not last.exists():
                raise LLMError(f"codex exited {out.returncode}: {(out.stderr or out.stdout).strip()[-600:]}")
            data = json.loads(last.read_text())
        return LLMResult(_validate(data, schema), self.name, self.model, time.time() - t0)


def _strict(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI structured outputs require additionalProperties=false and every
    property listed as required, recursively."""
    s = dict(schema)
    if s.get("type") == "object" and "properties" in s:
        s["properties"] = {k: _strict(v) for k, v in s["properties"].items()}
        s["required"] = list(s["properties"])
        s["additionalProperties"] = False
    if s.get("type") == "array" and isinstance(s.get("items"), dict):
        s["items"] = _strict(s["items"])
    return s


class OpenAICompatibleProvider(LLMProvider):
    name = "openai-compatible"

    def __init__(self) -> None:
        s = get_settings()
        self.base_url = (s.llm_base_url or "http://localhost:11434/v1").rstrip("/")
        self.model = s.llm_model or "qwen2.5:14b"

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], task: str) -> LLMResult:
        key = secrets.get("llm")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {"model": self.model, "temperature": 0.2,
                "messages": [{"role": "system", "content": system + "\nRespond with JSON only, matching this "
                              f"JSON Schema:\n{json.dumps(schema)}"},
                             {"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}}
        t0 = time.time()
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=600)
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"cannot reach {self.base_url}: {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"{self.base_url} returned {r.status_code}: {r.text[:300]}")
        payload = r.json()
        text = payload["choices"][0]["message"]["content"]
        try:
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"model did not return JSON: {text[:200]}") from e
        return LLMResult(_validate(data, schema), self.name, self.model, time.time() - t0,
                         {"input_tokens": (payload.get("usage") or {}).get("prompt_tokens", 0),
                          "output_tokens": (payload.get("usage") or {}).get("completion_tokens", 0)})


PROVIDERS: dict[str, type[LLMProvider]] = {
    "heuristic": HeuristicProvider, "claude-cli": ClaudeCLIProvider, "codex-cli": CodexCLIProvider,
    "openai-compatible": OpenAICompatibleProvider,
}


def get_llm(name: str | None = None) -> LLMProvider:
    name = name or get_settings().llm
    if name not in PROVIDERS:
        raise ValueError(f"unknown LLM provider {name!r}; available: {sorted(PROVIDERS)}")
    return PROVIDERS[name]()


def call(system: str, prompt: str, schema: dict[str, Any], task: str, provider: str | None = None,
         campaign_id: int | None = None, source_id: int | None = None) -> LLMResult:
    """Run one structured call and account for it. Raises LLMUnavailable when
    no model is configured, so callers can fall back to deterministic code."""
    llm = get_llm(provider)
    result = llm.complete_json(system, prompt, schema, task)
    tokens = float(result.usage.get("input_tokens", 0) + result.usage.get("output_tokens", 0))
    costs.record("llm", quantity=tokens, unit="tokens", campaign_id=campaign_id, source_id=source_id,
                 provider=result.provider, model=result.model, task=task, seconds=round(result.seconds, 1),
                 notional_usd=result.usage.get("notional_usd"))
    return result
