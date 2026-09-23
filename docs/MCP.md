# MCP server

`ezra mcp` runs an MCP server named `ezra` on stdio (`src/ezra/mcp_server.py`, built on the MCP
Python SDK). It exposes the same services as the CLI and API, so an agent (Claude Code, Codex,
Claude Desktop, any MCP client) can run a campaign end to end while the human keeps the approve
and publish decisions.

The server starts an embedded job worker thread, so queued analyses and renders progress while the
agent polls. Set `EZRA_MCP_WORKER=0` if a separate `ezra worker` is running against the same
database.

## Registering it

- **Claude Code**: this repo's `.mcp.json` registers it (`uv run ezra mcp`); approve the server
  when prompted. Tools appear as `mcp__ezra__ezra_*`. The operating guide is the `ezra` skill
  (`skills/ezra/SKILL.md`).
- **Codex** (`~/.codex/config.toml`):
  ```toml
  [mcp_servers.ezra]
  command = "uv"
  args = ["run", "--directory", "/path/to/Ezra", "ezra", "mcp"]
  ```
- **Any client**: command `uv`, args `["run", "--directory", "/path/to/Ezra", "ezra", "mcp"]`, with
  `EZRA_HOME` / `EZRA_DATABASE_URL` in the environment if not using `./data`.

## Workflow the server announces (its `instructions`)

1. `ezra_list_campaigns` / `ezra_get_campaign` (rules, CPM, thresholds) or `ezra_create_campaign`.
2. `ezra_add_source` (authorized footage) → `ezra_analyze_source` → poll `ezra_job_status`.
3. `ezra_find_candidates` → poll → `ezra_list_candidates` (ranked; FAIL excluded). Optionally read
   `ezra_get_transcript`, propose moments with `ezra_create_candidate`, and score any candidate
   per factor with `ezra_score_candidate` (the agent judges; Ezra weights and checks compliance).
4. `ezra_render_top` or `ezra_render_candidate` → poll → `ezra_list_clips`.
5. Show the human the clips and **ask** which to approve; `ezra_approve_clip` / `ezra_reject_clip`
   only with the human's explicit choice.
6. `ezra_generate_metadata`, then `ezra_publish_clip` (dry run) → `confirm=true` only after the
   human says yes.
7. `ezra_sync_metrics`, `ezra_campaign_report` / `ezra_earnings_report`, `ezra_optimize_campaign`.

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `ezra_list_campaigns` | – | Campaigns (CPM, thresholds, platforms) |
| `ezra_get_campaign` | `campaign` | Campaign with every rule and severity |
| `ezra_create_campaign` | `definition` (YAML/JSON/CSV text), `format?` | Created/updated campaigns |
| `ezra_add_source` | `path`, `campaign?`, `rights_basis`, `title?` | Source (path must be under `EZRA_IMPORT_ROOTS` when set) |
| `ezra_list_sources` | `campaign?` | Sources |
| `ezra_analyze_source` | `source_id`, `force?` | Job |
| `ezra_get_analysis` | `source_id` | Analysis summary (speakers, scenes, faces/layouts, silence, topics) |
| `ezra_get_transcript` | `source_id`, `start?`, `max_chars?` | Timestamped, speaker-labelled lines; `next_start` for paging |
| `ezra_find_candidates` | `source_id`, `campaign?`, `max_candidates?` | Job |
| `ezra_list_candidates` | `campaign?`, `source_id?`, `top?`, `include_failed?` | Ranked candidates with scores, compliance, EV |
| `ezra_get_candidate` | `candidate_id` | Full detail incl. explanations, `opens_with`, `ends_with`, warnings |
| `ezra_create_candidate` | `source_id`, `start`, `end`, `title?`, `hook?`, `hook_type?`, `reason?` | Snapped, scored candidate + edge warnings |
| `ezra_score_candidate` | `candidate_id`, `scores` (nine factors 0–100), `notes?`, `hook?`, `title?`, `hook_type?`, `compliant?`, `compliance_notes?` | Re-ranked candidate (`scorer: agent+heuristic`) |
| `ezra_rank_candidates` | `campaign?`, `source_id?`, `use_model?` | Job |
| `ezra_render_candidate` | `candidate_id`, `spec?` | Job (spec: aspect, layout, caption_theme, punch_in, remove_silence, hook_overlay, …) |
| `ezra_render_top` | `campaign?`, `source_id?`, `top?`, `spec?` | Job |
| `ezra_list_clips` | `campaign?`, `status?` | Clips with current version |
| `ezra_review_queue` | `campaign?` | Review cards (scores, reasons, compliance, EV, video path) |
| `ezra_approve_clip` | `clip_id`, `notes?` | Clip (**human decision only**) |
| `ezra_reject_clip` | `clip_id`, `reason?` | Clip |
| `ezra_update_clip` | `clip_id`, `title?`, `description?`, `hashtags?`, `platform_metadata?`, `rerender?` | Clip (or a re-render job) |
| `ezra_generate_metadata` | `clip_id`, `platforms?` | Per-platform copy + publish-stage compliance |
| `ezra_export_clip` | `clip_id` | Paths of mp4, thumbnail, SRT, ASS, JSON |
| `ezra_publish_clip` | `clip_id`, `platforms?`, `visibility?`, `confirm?` | Dry run (problems + plan) or queued posts |
| `ezra_schedule_clip` | `clip_id`, `schedule_at` (ISO), `timezone?`, `platforms?`, `visibility?`, `confirm?` | Same, scheduled |
| `ezra_list_posts` | `campaign?`, `status?` | Posts with latest metrics |
| `ezra_sync_metrics` | `campaign?` | Job |
| `ezra_record_metrics` | `post_id`, `views`, `likes?`, `comments?`, `shares?`, `saves?` | Snapshot |
| `ezra_campaign_report` | `campaign` | Earnings, costs, margin, observations |
| `ezra_earnings_report` | `campaign` | Qualified views, estimated vs confirmed revenue |
| `ezra_optimize_campaign` | `campaign`, `apply?` | Suggested weights (written only with `apply=true`) |
| `ezra_run_campaign` | `campaign`, `render_top?`, `max_candidates?` | Job for the supervised loop |
| `ezra_job_status` | `job_id`, `logs?` | Status, progress, result or structured error |
| `ezra_retry_job` | `job_id` | Re-queued job |

## Where the agent adds value

Ezra's heuristics are a baseline (confidence 0.35). An agent reading the whole transcript can:

- propose moments the sentence windows miss (`ezra_create_candidate`). Check `opens_with` and
  `ends_with`: a cut should open on its hook and end after its payoff;
- score candidates against each other per factor (`ezra_score_candidate`). Agent scores replace
  the heuristic factors, and Ezra still applies the campaign weights, performance prior,
  diversity penalty and compliance;
- judge rules a keyword check can't (forbidden topics, free-form rules) via `compliant` and
  `compliance_notes`.

## Safety

Approval and publishing are human decisions:

- Tool descriptions say so, and the instructions tell the agent to ask.
- `ezra_publish_clip` is a dry run without `confirm=true`, and refuses unapproved clips.
- Claude Code asks the user before each MCP tool call unless the user has allowed that tool.
  Don't pre-allow `ezra_approve_clip` or `ezra_publish_clip`.
- Every approval (actor `mcp-human`) and publish (actor `mcp`) is recorded in the audit log.
