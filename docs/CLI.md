# CLI

`ezra` (Typer). Every command works against the configured database and storage (`EZRA_HOME`,
`EZRA_DATABASE_URL`, `EZRA_STORAGE`), the same ones the API and MCP server use. `ezra COMMAND --help`
shows full option help.

Long operations (`analyze`, `find`, `rank`, `render`, `clip rerender/variant/broll`, `run`,
`live start`) run **inline** with a progress bar, in an isolated child process like a worker would.
Pass `--queue` to enqueue the job for a running worker instead.

## Typical session

```bash
ezra init
ezra campaign import campaigns/demo-campaign.yaml
ezra source add episode.mp4 --campaign demo --rights campaign_supplied
ezra run demo --top 5              # analyze → candidates → rank → render top 5
ezra review --campaign demo        # a approve · r reject · s skip · q quit (--open plays each clip)
ezra clip metadata 3 --platforms tiktok,youtube
ezra accounts add tiktok local-export me
ezra publish --clip 3 --platforms tiktok --dry-run
ezra publish --clip 3 --platforms tiktok             # asks before posting; --yes skips the prompt
ezra publish --clip 3 --platforms youtube --schedule "2026-10-01 18:00" --tz America/Toronto
ezra metrics sync                  # or: ezra metrics add POST_ID VIEWS / ezra metrics import file.csv
ezra report demo
ezra insights --campaign demo
ezra optimize demo                 # --apply writes the suggested weights
```

## Commands

| Command | Arguments / options | What it does |
|---|---|---|
| `ezra accounts add` | PLATFORM PROVIDER HANDLE --credential-ref --tz --export-dir | Register an account (local-export needs no credential; upload-post uses UPLOAD_POST_API_KEY). |
| `ezra accounts connect` | PROVIDER | Start OAuth for youtube |
| `ezra accounts list` | – | Publishing accounts and whether their credential is present |
| `ezra analyze` | --source --campaign --force --queue | Transcribe, diarize and analyze scenes, faces, silence and topics. |
| `ezra approve` | CLIP_ID --notes | Approve a rendered clip for publishing (FAIL-compliance clips are refused). |
| `ezra audit-log` | --limit | Recent audit events: approvals, publishes, rights changes, secret writes. |
| `ezra benchmark` | --out --fixtures | Build fixtures and run the end-to-end quality benchmark. |
| `ezra brandkit create` | PATH | Create/update a brand kit from YAML (name, logo, colors, caption_theme, ...). |
| `ezra brandkit list` | – | Brand kits |
| `ezra campaign create` | --name --cpm --min-views --max-payout --hashtags --platforms --min-duration --max-duration --brief | Create a campaign interactively. |
| `ezra campaign import` | PATH --no-sources | Import campaigns from YAML, JSON or CSV. |
| `ezra campaign list` | – | Campaigns with CPM, threshold, cap, budget |
| `ezra campaign show` | REF | One campaign with its rules and severities |
| `ezra candidate` | CANDIDATE_ID | Full detail for one candidate: transcript, factor scores, explanations, compliance, EV. |
| `ezra candidates` | --campaign --source --top --all | Ranked candidates (generated on first use; `ezra find` to regenerate). |
| `ezra clip broll` | CLIP_ID --provider --max-inserts --queue | Add rights-cleared B-roll (library |
| `ezra clip export` | CLIP_ID --dest | Export an approved clip: mp4, thumbnail, SRT, ASS, metadata JSON. |
| `ezra clip list` | --campaign --status | Clips with status, duration, layout |
| `ezra clip metadata` | CLIP_ID --platforms --no-model | Generate per-platform titles, captions and hashtags. |
| `ezra clip rerender` | CLIP_ID --aspect --theme --layout --punch-in --queue | New version with changed aspect/theme/layout/punch-in |
| `ezra clip show` | CLIP_ID | One clip with versions, copy and review notes |
| `ezra clip variant` | CLIP_ID PLATFORM --queue | Platform variant (e.g. linkedin 1:1, x 16:9). |
| `ezra cost` | --campaign | Recorded processing costs (transcription, analysis, render, storage, model calls). |
| `ezra db revision` | --message | Autogenerate a migration from model changes (review the file before committing). |
| `ezra db upgrade` | – | Apply migrations up to head (SQLite or PostgreSQL, per EZRA_DATABASE_URL). |
| `ezra earnings` | CAMPAIGN | Qualified views, estimated and confirmed payout for a campaign (JSON). |
| `ezra experiment analyze` | EXPERIMENT_ID | Per-variant results, P(best), recommendation |
| `ezra experiment assign` | EXPERIMENT_ID CLIP_ID | Assign a clip to the experiment's next variant |
| `ezra experiment create` | CAMPAIGN NAME FACTOR --values --hypothesis --min-samples | A/B test one factor (e.g. caption_theme) across values |
| `ezra find` | --source --campaign --max-candidates --queue | Generate and rank candidate moments. |
| `ezra init` | – | Create the data directory and run migrations. |
| `ezra insights` | --campaign | Cohorts, calibration and regression over published results. |
| `ezra jobs cancel` | JOB_ID | Cancel a queued or running job |
| `ezra jobs list` | --status --limit | Recent jobs with status and progress |
| `ezra jobs retry` | JOB_ID | Re-queue a failed or cancelled job |
| `ezra jobs show` | JOB_ID | One job with logs and error |
| `ezra live start` | CAMPAIGN --file --url --kind --speed --chunk-seconds --window-seconds --max-seconds --render-top --queue | Clip a live stream (or replay a file as one) into the review queue. |
| `ezra mcp` | – | Run the MCP server on stdio (with an embedded worker). |
| `ezra metrics add` | POST_ID VIEWS --likes --comments --shares --saves --payout | Record a snapshot by hand (e.g. from a campaign dashboard). |
| `ezra metrics import` | PATH | CSV with post_id or url plus views, likes, comments, shares, saves, ... |
| `ezra metrics sync` | --campaign | Pull metrics from each platform API for published posts. |
| `ezra optimize` | CAMPAIGN --apply | Suggest (or --apply) scoring weights learned from this campaign's results. |
| `ezra posts` | --campaign --status | Posts with status, visibility, latest views and URL. |
| `ezra publish` | --clip --campaign --platforms --private --schedule --tz --yes --dry-run | Publish approved clips (asks before posting). |
| `ezra rank` | --campaign --no-model --queue | Re-rank candidates (model critique when EZRA_LLM is configured). |
| `ezra reject` | CLIP_ID --reason | Reject a clip, with an optional reason kept for learning. |
| `ezra render` | --top --candidate --campaign --aspect --theme --layout --punch-in --no-silence-removal --no-filler-removal --queue | Render clips (9:16 by default) with tracking, captions and edits. |
| `ezra report` | CAMPAIGN | Campaign earnings, costs, margin and what is working. |
| `ezra review` | --campaign --open | Keyboard review: a = approve, r = reject, s = skip, q = quit. |
| `ezra run` | CAMPAIGN --top --max-candidates --autonomous --queue | analyze → candidates → rank → render top N → review queue (→ publish with --autonomous). |
| `ezra secrets list` | – | Stored secret names (never values) |
| `ezra secrets set` | REF --value | Store a secret (JSON or string) in the encrypted store. |
| `ezra serve` | --host --port --embedded-worker | Run the API (and, by default, a worker) for local use. |
| `ezra source add` | PATH --campaign --rights --title | Ingest a local file (or URL with yt-dlp) you are authorized to use. |
| `ezra source list` | --campaign | Sources with rights status and analysis state |
| `ezra source rights` | SOURCE_ID STATUS --basis --notes | Set rights status (authorized / unverified / rejected) with basis and notes (audited) |
| `ezra transcript` | SOURCE_ID --start | Print the timestamped, speaker-labelled transcript. |
| `ezra worker` | – | Run the job worker. |

## Notes

- `ezra candidates` generates candidates on first use for a campaign's analyzed sources; `ezra find`
  regenerates Ezra's own windows (candidates already rendered, and ones proposed by an agent, a
  person or OpenShorts, are kept).
- `ezra render` options map onto the render spec: `--aspect 9:16|1:1|16:9|4:5`, `--theme
  clean|bold|karaoke|cinematic|minimal|high-impact`, `--layout auto|track|split|blur|center`,
  `--punch-in`, `--no-silence-removal`, `--no-filler-removal`. Spec fields without a flag (for
  example `protect_graphics: false` to let crops ignore on-screen text,
  `yield_to_source_captions: false` to keep Ezra's captions over the source's own, or
  `caption_emoji`) go
  through a brand kit or the API/MCP `spec`.
- `ezra publish --private` posts with the most private visibility each platform supports (TikTok
  SELF_ONLY, YouTube private; Instagram has no private posts and refuses).
- `ezra run --autonomous` publishes only when the campaign has `human_approval_required: false` and
  `EZRA_ALLOW_AUTONOMOUS=1`; otherwise it stops at the review queue and says why.
- Exit codes: 0 on success; 1 on an expected error (unknown id, invalid input, missing credential,
  refused action), printed as one `error:` line that says what to fix; `--help` and usage errors 2.
