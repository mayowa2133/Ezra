"""Run a headless agent session (Claude Code or Codex) wired to clipper's MCP
server. The agent uses your Claude/ChatGPT subscription through its own CLI;
clipper never calls a model API itself.

Each phase gets only the tools it needs: review_clips and publish_clip are
blocked, so a headless session can never approve or post anything.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from ..config import settings

BLOCKED = ["review_clips", "publish_clip"]


def mcp_server_spec() -> dict:
    return {"command": sys.executable, "args": ["-m", "clipper.mcp_server"],
            "env": {"CLIPPER_HOME": str(settings().home)}}


def mcp_config() -> dict:
    return {"mcpServers": {"clipper": mcp_server_spec()}}


def _claude_cmd(prompt: str, config_path: Path, model: str | None) -> list[str]:
    cmd = ["claude", "-p", prompt, "--mcp-config", str(config_path), "--strict-mcp-config",
           "--allowedTools", "mcp__clipper",
           "--disallowedTools", *[f"mcp__clipper__{t}" for t in BLOCKED],
           "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]
    return cmd


def _codex_cmd(prompt: str, model: str | None) -> list[str]:
    spec = mcp_server_spec()
    env = ", ".join(f'{k} = "{v}"' for k, v in spec["env"].items())
    cmd = ["codex", "exec", "--skip-git-repo-check",
           "-c", f'mcp_servers.clipper.command="{spec["command"]}"',
           "-c", f"mcp_servers.clipper.args={json.dumps(spec['args'])}",
           "-c", f"mcp_servers.clipper.env={{ {env} }}",
           "-c", f"mcp_servers.clipper.disabled_tools={json.dumps(BLOCKED)}",
           "-c", 'approval_policy="never"', "--sandbox", "read-only"]
    if model:
        cmd += ["--model", model]
    return [*cmd, prompt]


def _describe_tool(name: str, args: dict) -> str:
    name = name.removeprefix("mcp__clipper__")
    if name == "read_transcript":
        return f"reading transcript (source {args.get('source_id')}, from {args.get('start', 0):.0f}s)"
    if name == "add_candidates":
        return f"proposing {len(args.get('candidates') or [])} candidate moments"
    if name == "score_clips":
        return f"scoring {len(args.get('scores') or [])} clips"
    if name == "set_clip_copy":
        return f"writing copy for clip {args.get('clip_id')}"
    return name


def run(prompt: str, agent: str | None = None, model: str | None = None,
        on_event: Callable[[str], None] | None = None) -> str:
    """Run one agent task to completion; returns the agent's final text."""
    agent = agent or settings().agent
    if not shutil.which(agent):
        raise RuntimeError(f"`{agent}` CLI not found on PATH (install Claude Code or Codex, or pass --agent)")
    say = on_event or (lambda _m: None)
    if agent == "codex":
        result = subprocess.run(_codex_cmd(prompt, model), capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"codex exited {result.returncode}: {result.stderr.strip()[-800:]}")
        return result.stdout.strip()
    if agent != "claude":
        raise ValueError("agent must be 'claude' or 'codex'")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(mcp_config(), fh)
        config_path = Path(fh.name)
    final, errors = "", []
    try:
        proc = subprocess.Popen(_claude_cmd(prompt, config_path, model), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        say(_describe_tool(block.get("name", ""), block.get("input") or {}))
            elif event.get("type") == "result":
                final = event.get("result") or ""
                if event.get("is_error"):
                    errors.append(final or event.get("subtype", "error"))
        proc.wait()
        if proc.returncode != 0 or errors:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"claude exited {proc.returncode}: {(errors or [stderr.strip()[-800:]])[0]}")
    finally:
        config_path.unlink(missing_ok=True)
    return final
