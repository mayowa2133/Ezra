# REST API

FastAPI app in `src/ezra/api/app.py`. Run it with `ezra serve` (port 8000, embedded worker) or the
Docker `api` service. The OpenAPI schema and a try-it UI are at `/docs` (`/openapi.json`).

## Conventions

- **Auth**: when `EZRA_API_TOKEN` is set, every `/api/*` route except `/api/health`, `/api/media`
  (signed links) and the OAuth callback requires `Authorization: Bearer <token>`. The dashboard's
  server-side proxy adds it; browsers never see the token.
- **Long operations return a job** (`202` with the job object). Poll `GET /api/jobs/{id}` for
  `status` (queued, running, completed, failed, cancelled), `progress`, `message`, `result` and a
  structured `error` (type, message, attempt).
- **Errors**: `400` invalid input (`ValueError`), `401` missing/wrong token, `403` forbidden (e.g.
  approving a FAIL clip, a bad media signature), `404` unknown entity, `413` upload too large,
  `415` unsupported file type, `416` bad range, `424` a required credential is missing (the message
  names the env var), `429` rate limited, `502` a platform API failed. Bodies are
  `{"detail": "..."}`.
- **Media**: clip JSON carries `video_url`, `thumbnail_url` and `captions_url`. These are signed,
  expiring links (`/api/media?key=…&exp=…&sig=…`, HMAC with `EZRA_SECRET_KEY`) that support HTTP
  range requests, so `<video>` can seek.
- Security headers on every response; CORS allows `EZRA_WEB_URL`; per-client rate limit
  `EZRA_RATE_LIMIT_PER_MINUTE`.

## Routes

| Method | Path | Does |
|---|---|---|
| GET | `/api/health` | Liveness + version (no auth) |
| GET | `/api/dashboard` | Counts: campaigns, sources, candidates, review pending, posts, views, estimated earnings, running jobs |
| GET | `/api/campaigns` | List campaigns |
| POST | `/api/campaigns` | Create/update from a JSON `CampaignSpec` |
| POST | `/api/campaigns/import` | `{text, format?}`: YAML/JSON/CSV text → campaigns |
| GET | `/api/campaigns/{ref}` | Campaign with rules (`ref` = slug or id) |
| POST | `/api/campaigns/{ref}/rules` | Add a manual rule `{kind, params, severity, description}` |
| GET | `/api/campaigns/{ref}/earnings` | Qualified views, estimated vs confirmed revenue, remaining budget |
| GET | `/api/campaigns/{ref}/report` | Earnings + costs + margin + actionable observations |
| GET | `/api/campaigns/{ref}/insights` | Cohorts, calibration, regression, learnings |
| POST | `/api/campaigns/{ref}/optimize` | Suggested weights (`?apply=true` writes them) |
| POST | `/api/campaigns/{ref}/revenue` | Record money received `{amount, post_id?, notes?}` |
| POST | `/api/campaigns/{ref}/run` | Job: analyze → candidates → rank → render top N |
| GET | `/api/sources` | List sources (`?campaign=`) |
| POST | `/api/sources` | Multipart upload: `file`, `campaign`, `rights_basis`, `title` (validated, size-limited, streamed to disk) |
| POST | `/api/sources/import` | `{path, campaign, rights_basis}` for files under `EZRA_IMPORT_ROOTS` |
| GET | `/api/sources/{id}` | Source with analysis summary |
| POST | `/api/sources/{id}/rights` | `{status: authorized|unverified|rejected, basis, notes}` (audited) |
| POST | `/api/sources/{id}/analyze` | Job: transcription, diarization, scenes, faces, silence, topics (`?force=true` redoes) |
| GET | `/api/sources/{id}/analysis` | All analysis products with provider/version/confidence |
| GET | `/api/sources/{id}/transcript` | Paged transcript (`start`, `max_chars`; follow `next_start`) |
| POST | `/api/sources/{id}/candidates` | Job: generate + rank candidates `{campaign?, max_candidates}` |
| GET | `/api/candidates` | Ranked candidates (`campaign`, `source_id`, `top`, `include_failed`) |
| GET | `/api/candidates/{id}` | Full candidate: transcript, context, factor scores + explanations, compliance, EV, opens/ends with |
| POST | `/api/candidates` | Custom candidate `{source_id, start, end, title?, hook?, reason?}` (snapped to words, scored) |
| POST | `/api/candidates/{id}/scores` | Agent factor scores `{scores: {hook…campaign_fit}, notes, compliant}`; Ezra applies weights |
| POST | `/api/candidates/rank` | Job: re-rank (`use_model` for the LLM critic) |
| POST | `/api/candidates/{id}/render` | Job: render with an optional `spec` (aspect, layout, caption_theme, punch_in, …) |
| POST | `/api/render/top` | Job: render the N best publishable unrendered candidates |
| GET | `/api/clips` | Clips (`campaign`, `status`) with current version + media links |
| GET | `/api/clips/{id}` | Clip with all versions |
| GET | `/api/review` | Review cards: video, scores, reasons, compliance, EV, transcript, copy |
| POST | `/api/clips/{id}/approve` | Human approval `{reviewer, notes}` (403 for FAIL compliance) |
| POST | `/api/clips/{id}/reject` | `{reviewer, notes}` |
| PATCH | `/api/clips/{id}` | Edit title/description/hashtags/`platform_metadata` |
| POST | `/api/clips/{id}/metadata` | Generate per-platform copy `{platforms?, use_model}` |
| POST | `/api/clips/{id}/rerender` | Job: new version with `{changes}` to the spec |
| POST | `/api/clips/{id}/variant/{platform}` | Job: platform preset variant (e.g. `linkedin` 1:1) |
| POST | `/api/clips/{id}/broll` | Job: add B-roll `{provider, max_inserts}` |
| POST | `/api/clips/{id}/export` | Export mp4 + thumbnail + SRT + ASS + JSON |
| POST | `/api/clips/{id}/publish` | `{platforms, account_ids?, visibility, schedule_at?, timezone?, confirm}`: dry run unless `confirm: true`; returns `problems`, `plan`, `post_ids`, `job_ids` |
| GET | `/api/posts` | Posts with status, URL, latest metrics |
| POST | `/api/posts/{id}/cancel` | Cancel a scheduled post |
| POST | `/api/posts/{id}/retry` | Retry a failed post |
| POST | `/api/posts/{id}/metrics` | Manual snapshot `{views, likes, …, actual_payout?}` |
| GET | `/api/posts/{id}/metrics` | Snapshot history |
| POST | `/api/metrics/import` | `{text}`: CSV with `post_id` or `url` plus metric columns |
| POST | `/api/metrics/sync` | Job: pull metrics from platform APIs (`?campaign=`) |
| GET | `/api/accounts` | Publishing accounts (credential presence only, never values) |
| POST | `/api/accounts` | `{platform, provider, handle, credential_ref?, timezone, meta}` |
| GET | `/api/integrations` | Provider status: configured / missing (names the env var) |
| GET | `/api/integrations/{provider}/connect` | Start OAuth (PKCE); returns `authorize_url` |
| GET | `/api/integrations/{provider}/callback` | OAuth redirect target; stores tokens in the secret store, redirects to the dashboard |
| GET | `/api/experiments` | List experiments |
| POST | `/api/experiments` | `{campaign, name, factor, values, hypothesis, min_samples}` |
| POST | `/api/experiments/{id}/assign/{clip_id}` | Assign a clip to the next variant |
| GET | `/api/experiments/{id}/analysis` | Per-variant results, P(best), recommendation |
| GET | `/api/brand-kits` | List brand kits |
| POST | `/api/brand-kits` | Create/update a brand kit |
| GET | `/api/jobs` | Jobs (`status`, `kind`, `limit`) |
| GET | `/api/jobs/{id}` | Job with logs |
| POST | `/api/jobs/{id}/cancel` | Request cancellation (running child process is terminated) |
| POST | `/api/jobs/{id}/retry` | Re-queue a failed/cancelled job |
| POST | `/api/live` | Job: live clipping session `{campaign, url | file, kind, …}` |
| GET | `/api/settings` | Effective non-secret configuration |
| GET | `/api/audit` | Recent audit events |
| GET | `/api/media` | Signed, ranged media download |

## Example

```bash
T="Authorization: Bearer $EZRA_API_TOKEN"
curl -s -H "$T" -X POST localhost:8000/api/campaigns/import \
     -H 'content-type: application/json' --data "$(jq -Rs '{text: .}' campaigns/demo-campaign.yaml)"
curl -s -H "$T" -F file=@episode.mp4 -F campaign=demo -F rights_basis=campaign_supplied localhost:8000/api/sources
curl -s -H "$T" -X POST localhost:8000/api/sources/1/analyze          # → {"id": 1, "status": "queued", ...}
curl -s -H "$T" localhost:8000/api/jobs/1                              # poll until completed
curl -s -H "$T" -X POST localhost:8000/api/sources/1/candidates -H 'content-type: application/json' -d '{}'
curl -s -H "$T" "localhost:8000/api/candidates?campaign=demo&top=5"
```

`scripts/smoke_api.py` runs the whole flow (import → upload → analyze → candidates → render →
approve → local-export publish → metrics → earnings) against any running API.
