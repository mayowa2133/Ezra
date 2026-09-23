---
name: ezra
description: Operate Ezra, the clipping and performance-optimization system — import campaigns, ingest authorized footage, find and rank candidate moments, render clips, run the human review, generate metadata, publish, sync metrics and learn from results. Use when the user asks to clip footage, run a campaign, review or publish clips, check earnings, or ask what is performing.
---

# Operating Ezra

Ezra's MCP tools are named `ezra_*` (server `ezra`, see `.mcp.json`). The CLI (`uv run ezra ...`) exposes the
same services; prefer the MCP tools inside an agent session. Long operations return a job: poll
`ezra_job_status(job_id)` until `completed`, and read `error` if it failed (then `ezra_retry_job`).

## Run a campaign

1. `ezra_list_campaigns` / `ezra_get_campaign`: read the rules (severity fail / review / info), CPM,
   minimum qualified views and caps. New campaign: `ezra_create_campaign` with YAML
   (see `campaigns/demo-campaign.yaml`).
2. `ezra_add_source` with the footage path and `rights_basis`. Never ingest footage without a rights
   basis; ask the user if unsure. Then `ezra_analyze_source`.
3. `ezra_find_candidates`, then `ezra_list_candidates`. FAIL candidates are excluded by default.
4. Optional, and where you add the most value: read `ezra_get_transcript` for the whole source and
   - propose moments Ezra's heuristics missed with `ezra_create_candidate` (check the returned
     `opens_with`, `ends_with`, `warnings`; a cut must open on its hook and end after its payoff);
   - score candidates with `ezra_score_candidate` per factor (0-100, compared against each other;
     put the comparison in `notes`). Ezra applies campaign weights, the performance prior and the
     diversity penalty; never pre-weight.
5. `ezra_render_top` (or `ezra_render_candidate` with a spec: aspect, layout, caption_theme, punch_in).
6. `ezra_review_queue`: show the user each clip — title, rank, duration, compliance status and
   reasons, expected value with its p10–p90 range and basis — and ASK which to approve.
   `ezra_approve_clip` / `ezra_reject_clip` only for the clips the user chose.
7. `ezra_generate_metadata`, then `ezra_publish_clip` (dry run). Show exactly what will post; call it
   again with `confirm=true` only after the user says yes. `ezra_schedule_clip` for later posting.

## Learn from results

- `ezra_sync_metrics` (platform APIs) or `ezra_record_metrics` (numbers the user reads off a
  dashboard). Then `ezra_campaign_report` / `ezra_earnings_report`.
- `ezra_optimize_campaign` suggests scoring weights from observed results; apply only with the
  user's agreement. Treat small samples as weak evidence and say so.

## Never

- approve, reject, publish or schedule without the user's explicit decision in this conversation;
- call a score a prediction of virality: it is a ranking estimate with a stated confidence;
- ingest or clip footage the user has no rights to use.
