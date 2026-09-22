# clipper: agent guide

Local clipping operator: campaign footage in, approved short clips out, performance fed back into
the judge. No SaaS, no accounts, no dashboard. The CLI plus MCP is the product.

**The AI never runs inside clipper.** Judgment (finding moments, scoring, copy, analysis) is done
by the agent driving the `clipper` MCP server: Claude Code, Codex, or a headless session started by
`clipper run`. clipper never calls a model API. Don't add one.

## Map

| Path | Owns |
|---|---|
| `src/clipper/campaigns.py` | Campaign YAML → `CampaignSpec`, rubric weights (renormalised), upsert/lookup by slug |
| `src/clipper/clipping/sources.py` | Footage per campaign, ffprobe duration, optional yt-dlp download |
| `src/clipper/clipping/transcribe.py` | Local faster-whisper with word timestamps (`uv sync --extra local`) |
| `src/clipper/clipping/transcript.py` | Transcript JSON, paging for agents, **snapping cuts to word edges**, token joining |
| `src/clipper/clipping/clips.py` | Clip lifecycle and status machine (candidate → scored → rendered → approved → published) |
| `src/clipper/clipping/render.py` | ffmpeg 1080x1920 render; captions/hook card drawn with Pillow and overlaid (no libass needed) |
| `src/clipper/ranking/rubric.py` | The judging brief: rules, rubric dimensions and weights, hook types, learnings |
| `src/clipper/ranking/scoring.py` | `ClipScore` and the weighted `ai_score` (weights applied here, never by the agent) |
| `src/clipper/ranking/compliance.py` | Deterministic campaign checks on clips and on copy |
| `src/clipper/publishing/publisher.py` | Human-approval + copy gate, Upload-Post posting, status refresh, metrics sync |
| `src/clipper/integrations/uploadpost.py` | Upload-Post REST client |
| `src/clipper/integrations/openshorts.py` | Optional self-hosted OpenShorts: its moments become candidates, its renders are reusable |
| `src/clipper/analytics/` | Metric snapshots; insights (trait lift, judge calibration) and learnings |
| `src/clipper/revenue.py` | qualified views, CPM, per-post cap, campaign budget |
| `src/clipper/agents/` | Headless agent runner (`claude -p` / `codex exec`) and the task prompts |
| `src/clipper/pipeline.py` | `clipper run`: the full loop with terminal approval and publish prompts |
| `src/clipper/mcp_server.py` | The MCP tools; `INSTRUCTIONS` there is the canonical workflow |
| `skills/clipping/SKILL.md` | How an agent should run a campaign interactively |
| `campaigns/` | Campaign YAML files |
| `data/` (gitignored) | SQLite DB, transcripts, renders; override with `CLIPPER_HOME` |

Tables: `campaigns`, `sources`, `clips`, `posts`, `metrics`, plus `learnings` (see `db.py`).

## Invariants: don't break these

- Nothing is approved or published without a human decision. `review_clips` and `publish_clip`
  are blocked in headless sessions (`agents/runner.py`), and `publish` refuses clips that aren't
  `approved` or whose copy fails compliance.
- Agents score dimensions; `ranking/scoring.py` applies weights. Keep it that way so scores are
  comparable across runs and the calibration check means something.
- Clip boundaries always pass through `transcript.snap` (OpenShorts-origin clips excepted: their
  render uses OpenShorts' exact bounds).
- Every DB access opens its own connection (`db.connect()`); the MCP server works on threads.

## Working here

```bash
uv sync --extra local          # deps + faster-whisper
uv run pytest -q               # full suite (~15s; renders real video with ffmpeg)
uv run clipper --help
uv run clipper mcp             # the MCP server on stdio
```

Tests use a synthetic ffmpeg video and a fake transcript; `tests/test_mcp_stdio.py` drives the real
server over stdio. Add a test with every behaviour change. Commit in small, working slices.

Codex: register the server with `uv run clipper mcp-config --codex >> ~/.codex/config.toml`.
