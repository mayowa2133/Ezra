@AGENTS.md

## Claude Code specifics

- `.mcp.json` registers the `ezra` MCP server (`uv run ezra mcp`); its tools appear as
  `mcp__ezra__ezra_*` once you approve the server. The server runs an embedded job worker unless
  `EZRA_MCP_WORKER=0`.
- The `ezra` skill (`skills/ezra/SKILL.md`, linked into `.claude/skills/`) is the workflow for
  running a campaign. Load it when the user asks to clip, review, publish or analyse results.
- Approving and publishing are the human's decisions: show the review cards and the publish dry
  run, and call `ezra_approve_clip` / `ezra_publish_clip(confirm=true)` only for what the user chose.
- `.claude/launch.json` defines `ezra-api` (:8000) and `ezra-web` (:3000) preview servers; set
  `EZRA_DEMO_HOME` to point the API at a different data directory.
- `EZRA_LLM=claude-cli` makes Ezra's critic call `claude -p` headlessly (no tools, JSON schema output).
  A 401 there means the CLI needs `claude` → `/login` again.
