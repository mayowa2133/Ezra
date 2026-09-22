@AGENTS.md

## Claude Code specifics

- `.mcp.json` registers the `clipper` MCP server for this repo (`uv run clipper mcp`), so its
  tools show up as `mcp__clipper__*` once you approve the server.
- The `clipping` skill (`skills/clipping/SKILL.md`, linked into `.claude/skills/`) is the workflow
  for running a campaign. Load it when the user asks to clip, review, publish or analyse.
- `clipper run <campaign>` starts its own headless `claude -p` sessions with this same server; if
  that fails with a 401, the user needs to sign in again (`claude`, then `/login`).
