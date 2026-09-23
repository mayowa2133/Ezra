# Data model

SQLAlchemy 2 models in `src/ezra/db/models.py`, migrated with Alembic (`src/ezra/db/migrations`).
The same schema runs on SQLite (local) and PostgreSQL (Docker); JSON columns hold structured
detail (rules params, features, explanations, edit summaries, raw platform responses).
Timestamps are UTC.

```
brand_kits ─┐
            ▼
campaigns ──┬── campaign_rules
            ├── sources ──┬── transcripts ── transcript_segments (words JSON: w, s, e, p, spk)
            │             └── source_analyses (kind: scenes | faces | silence | topics | text_signals | speakers)
            ├── candidates ── clips ──┬── clip_versions
            │                         └── posts ──┬── metric_snapshots
            │                                     └── revenue_records
            ├── experiments ◄── posts.experiment_id
            └── cost_records
publish_accounts ◄── posts.account_id
jobs ── job_logs            learnings            audit_events
integration_credentials     oauth_states
```

## Tables

| Table | Purpose | Key columns |
|---|---|---|
| `campaigns` | A performance-paid programme | `slug` (unique), payout (`payout_type`, `cpm`, `min_qualified_views`, `max_payout_per_clip`, `budget`, `currency`, `tracking_window_days`), window (`starts_at`, `ends_at`), requirements (`allowed_platforms`, `min/max_duration`, `required_hashtags/mentions/cta`, `subtitles_required`, `logo_required`, `allowed_speakers`), prohibitions (`forbidden_words/topics`, `competitors`, `geography`), `posting_limits`, `human_approval_required`, `brief`, `weights`, `raw_instructions`, `brand_kit_id` |
| `campaign_rules` | One enforceable rule | `kind` (duration, source_rights, platforms, profanity, forbidden_words, competitors, forbidden_topic, hashtags, mentions, cta, subtitles, logo, speakers, window, posting_limits, geography, freeform), `params`, `severity` (fail / review / info), `origin` (normalized / manual) |
| `brand_kits` | Visual identity | logo, fonts, colors, caption theme, intro/outro, default layout, CTA, watermark, safe zone |
| `sources` | Long-form footage | `storage_key`, `sha256` (dedupe), media probe fields, `rights_status` (authorized / unverified / rejected), `rights_basis`, `rights_notes`, `status` (ingested → analyzing → analyzed / failed) |
| `transcripts` / `transcript_segments` | ASR output | provider, model, **version** (cache key), language; per sentence: text, speaker, confidence, `words` JSON |
| `source_analyses` | Analysis products | `kind`, `provider`, `version`, `confidence`, `data` JSON |
| `candidates` | A proposed moment | `start/end`, transcript, speakers, topic, hook/title/reason, `origin` (heuristic / agent / openshorts / live), nine `*_score` columns + `content_score`, `performance_prior`, `diversity_penalty`, `rank_score`, `confidence`, `scorer`, `score_explanations`, `compliance_status/reasons`, `expected_value`, `features`, `status` (new → ranked → rendered / discarded) |
| `clips` | A rendered clip under review | `status` (rendering → rendered → approved / rejected → published; failed), title, description, hashtags, `platform_metadata`, review notes, reviewer, `current_version_id` |
| `clip_versions` | One render | `version`, the full `spec`, keys for video/thumbnail/SRT/ASS, duration, size, `layout_used`, `edit_summary` (removed seconds, refined boundaries, per-scene layout and crop decisions, punch-ins, B-roll), `render_seconds`, `status`, `error` |
| `publish_accounts` | Where posts go | `platform`, `provider` (youtube, tiktok, instagram, upload-post, local-export), `handle`, **`credential_ref`** (never the secret), `timezone`, `status` |
| `posts` | One publication | clip + version + account, `platform`, `provider`, `external_id`, `url`, `status` (scheduled → publishing → published; failed; cancelled), `visibility`, caption/title, `scheduled_at` + `timezone`, `retries`, **`idempotency_key`** (unique), experiment + variant, **`features`** (traits snapshotted for analytics), `actual_payout`, `raw_response` |
| `metric_snapshots` | Views over time | `captured_at`, `provider` (platform / manual / csv), views, likes, comments, shares, saves, impressions, watch time, completion, followers gained, retention curve |
| `revenue_records` | Money actually received | campaign, post, kind, amount, currency, source |
| `cost_records` | What processing cost | kind (transcription, analysis, render, storage, llm, api), amount, quantity, unit, links to campaign/source/clip |
| `experiments` | A/B tests | factor, variants, hypothesis, `min_samples_per_variant`, status |
| `learnings` | Durable lessons | text, evidence, origin, active |
| `jobs` / `job_logs` | Queue | kind, status, priority, payload, result, error, progress, attempts/max, `cancel_requested`, `dedupe_key`, worker, `run_after`, heartbeat |
| `integration_credentials` | Metadata about stored credentials | provider, account label, **`secret_ref`**, scopes, expiry, status |
| `oauth_states` | OAuth CSRF state + PKCE verifier | single use, 10-minute lifetime, bound to a provider |
| `audit_events` | Who did what | actor, action (approve, reject, publish, rights change, secret write…), entity, details |

## Lifecycles

```
source:     ingested ─► analyzing ─► analyzed           (failed on error; re-run resumes)
candidate:  new ─► ranked ─► rendered                    (discarded by a person or agent)
            compliance: PASS | REVIEW_REQUIRED | FAIL    (FAIL: never rendered by render_top, never approvable)
clip:       rendering ─► rendered ─► approved ─► published
                              └────► rejected
job:        queued ─► running ─► completed | failed | cancelled
                 ▲        │ transient error, attempts left (backoff)
                 └────────┘
post:       scheduled ─► publishing ─► published
                              └─────► failed ─► (retry) publishing
            scheduled ─► cancelled
```

## Storage keys

Media lives in the storage backend under POSIX-style keys, never absolute paths:
`sources/{id}/original.mp4`, `clips/{clip}/v{n}/clip.mp4`, `…/thumbnail.jpg`,
`…/captions.srt`, `…/captions.ass`, `brand/{kit}/logo.png`, `broll/…`, `live/{session}/…`.
`storage.validate_key` rejects absolute paths, `..`, backslashes and NUL.
