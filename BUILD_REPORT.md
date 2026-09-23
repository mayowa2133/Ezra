# Build report

State of Ezra at the end of the build (2026-09-23). Everything marked **working** was run and
checked in this environment (macOS arm64 locally, Debian in Docker). Nothing is claimed from code
reading alone. Evidence is linked or named.

## Fully working (verified)

| Area | What works | Evidence |
|---|---|---|
| Campaigns | YAML (nested + flat), JSON, CSV import; normalized fields; 17 rule kinds with severities; manual rules survive re-import; brand kits | `test_campaigns_compliance.py`; `ezra campaign import` in a fresh clone |
| Sources | Upload (streamed, size-limited, ffprobe-validated), server-side import within allowed roots, sha256 dedupe, rights status + basis, audit | API/CLI/MCP e2e tests; Docker smoke test |
| Transcription | faster-whisper (batched) word timestamps, split tokens merged ("$400,000", "co-founder"), sentence segments, version-keyed cache | Benchmark WER 0.7–0.9%; turn onset error p50 62–85 ms |
| Diarization | Local MFCC clustering with a one-speaker default | Benchmark: 1/1, 2/2 speakers correct (98–100% word accuracy); 3-speaker case merged to 2 (see limitations) |
| Visual analysis | Scene cuts (PySceneDetect), faces (Haar + eye verification), per-scene layout hints, silence, topics, text signals | Benchmark: 29/29 cuts, face recall 92–100% |
| Candidates | Sentence-aligned windows, nine explained factor scores, topic-drift handling, interviewer-segue trimming, diversity, compliance, expected value | Benchmark: 15–18 candidates per 3-min source, 100% clean starts/ends, #1 opens on a strong moment on all fixtures |
| Agent judging | Custom candidates, agent factor scores, compliance flags via MCP | `test_mcp_workflow_over_stdio` |
| Model critique | `EZRA_LLM=claude-cli` structured calls with usage accounting | One live `claude -p --json-schema` call (docs/TESTING.md) |
| Rendering | 9:16/1:1/16:9/4:5, per-scene track/split/blur/center, waveform-refined cuts, silence/filler removal, 6 caption themes, hook card, logo, watermark, CTA, punch-in, loudness −14 LUFS, SRT/ASS, thumbnails, platform variants, re-render history | Benchmark: 12/12 renders pass all 10 checks; `test_real_render_split_layout_with_captions`; frames inspected visually |
| B-roll | Local library, Pexels, generated text cards; re-render with inserts | `test_broll_textcard_inserts_rerender_the_clip`; frame inspected |
| Review | CLI keyboard queue; dashboard queue (A/R/J/K/G/S/Space) with video, scores, reasons, compliance, EV, copy editing | Browser checks (local + Docker); API e2e |
| Metadata | Per-platform title/caption/hashtags with required tags/mentions/CTA enforced and length limits; publish-stage compliance | e2e tests; fresh-clone run |
| Publishing pipeline | Approval gate, dry run, idempotency, duplicate refusal, posting limits, campaign window, scheduling with timezones, scheduler, retries/cancel, local export | e2e tests incl. `test_scheduled_post_is_published_by_the_scheduler_when_due`; Docker smoke |
| Metrics & money | Manual/CSV/API metrics, qualified views, CPM, per-post cap, budget, tracking window, estimated vs confirmed, costs, margin | `test_economics_analytics.py`; `ezra report` in fresh clone ($24.00 on 12,000 views at $2 CPM) |
| Learning | Cohorts with shrinkage, calibration, ridge regression, actionable observations, learnings, experiments (P(best) with minimum samples), weight optimizer | `test_economics_analytics.py` |
| Live clipping | Rolling windows from a file replayed as a stream (HLS/YouTube sources share the path) | `test_live_session_clips_a_replayed_stream` |
| Jobs | DB queue, SKIP LOCKED on Postgres, retries with backoff, permanent errors, cancel (kills the child), stale requeue, dedupe, per-job child process | `test_core.py`, `test_postgres_s3.py` (6 concurrent claimers), benchmark 6/6 |
| Storage | Local and S3 (MinIO), traversal guards, signed ranged media | moto test; MinIO in Docker |
| Security | Bearer auth, signed media, rate limit, security headers, upload validation, import roots, encrypted secrets, OAuth state + PKCE, safe redirects, audit log | `test_security_diarization.py`, API e2e |
| Interfaces | CLI (58 commands), REST API (67 routes), MCP server (34 tools), Next.js dashboard (12 pages) | e2e tests over all four; browser checks |
| Deployment | Docker images (API/worker/CLI, web), compose with Postgres, MinIO, migrate, api, worker, web, test profile | Built and run; smoke test OK; full suite passes in the container |
| Benchmark | `ezra benchmark`: four fixtures with ground truth, compliance and job suites | BENCHMARK_REPORT.md |

## Requires your credentials (implemented, not exercised live)

| Integration | Needs | Status |
|---|---|---|
| YouTube Data API (upload, `publishAt`, stats, token refresh) | Google OAuth client, consent screen, API audit for public uploads | Implemented to the documented API; verified against mocks |
| TikTok Content Posting API (FILE_UPLOAD chunks, status, video query) | TikTok app with Login Kit + Content Posting; audit for public posts | Same |
| Instagram API with Instagram Login (REELS, rupload, insights) | Meta app, professional account, App Review for others' accounts | Same |
| Upload-Post | `UPLOAD_POST_API_KEY` + profile | Same |
| pyannote diarization | `HF_TOKEN` + accepted model terms (+ `uv pip install pyannote.audio`) | Implemented, not run |
| Pexels B-roll | `PEXELS_API_KEY` | Implemented, not run live |
| OpenShorts moments | A running OpenShorts + its own model key | Implemented; mocked at its REST API |

Setup steps: [REMAINING_EXTERNAL_SETUP.md](REMAINING_EXTERNAL_SETUP.md).

## Mocked in tests

- Platform APIs (YouTube, TikTok, Instagram, Upload-Post) via `httpx.MockTransport`, following the
  documented request/response shapes: OAuth code exchange and refresh, resumable/chunked uploads
  with exact `Content-Range`, status polling, metrics, 429/5xx retry classification, error payloads.
- S3 via moto in the local suite. (Real MinIO is used in the Docker suite and the stack.)
- OpenShorts REST API.
- No test posts to a real platform.

## Known limitations

1. **Heuristic moment selection finds half the labelled strong moments** as openings in the top 5
   (3/6 on every fixture). Two near-duplicate cuts of one story can both rank. The design expects an
   agent (MCP) or an LLM critic to close this gap. Without one, scores are a baseline (confidence 0.35).
2. **Local diarization is weak on 3+ speakers and similar voices** (third voice merged on `multi`,
   90% word accuracy). pyannote is the documented upgrade. Interview-specific logic (segue trimming)
   depends on correct speaker labels.
3. **Fixtures are synthetic.** TTS speech and still portraits are easier than real footage.
   Benchmark numbers are regression signals, not real-world accuracy.
4. **Face detection is Haar by default.** Frontal faces are reliable. Profiles, small faces and
   tight close-ups are weaker (92% recall on the close-up fixture). MediaPipe is available but not
   benchmarked.
5. **Text-card B-roll headlines are keyword-based** without a model ("companies profitable
   boring"). Numbers read well ("$2 million").
6. **Single-tenant.** One API token grants full access; there are no users, roles or per-user
   audit identity from the dashboard (the actor is "dashboard").
7. **Codex CLI and OpenAI-compatible LLM providers** are implemented but not run against a real
   model in this environment.
8. **No live posting was performed**, so platform-side behaviours (quota errors, audit
   restrictions, processing delays) are handled per documentation but unobserved.
9. **Metrics windows**: platform APIs return lifetime counts. Views are attributed to the tracking
   window using snapshots Ezra takes (`EZRA_METRICS_SYNC_HOURS`), so infrequent syncs blur the
   window edge.
10. **Inbound webhooks** aren't exposed (the signing helpers exist). Status comes from polling.

## Deferred improvements

- Active speaker detection (lip/voice activity per face) to choose the tracked face in multi-person
  shots, instead of the most prominent face.
- A learned ranking model once enough published history exists (the regression and calibration
  are in place to evaluate it).
- Marketplace API adapters behind `CampaignSourceAdapter` if programmes publish official APIs.
- Multi-user auth (OIDC) and per-user audit identity.
- Word-level forced alignment by default (WhisperX) once PyTorch weight is acceptable.
- Automatic thumbnail text overlays and A/B thumbnail experiments.
- A GPU Docker variant.

## Definition of Done audit

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Development stack starts | ✅ | `ezra serve` + `npm run dev` (preview servers); `docker compose up` (all services healthy) |
| 2 | Migrations run | ✅ | Alembic on SQLite and Postgres; `migrate` service; `test_migrations_match_the_models` |
| 3 | Campaign created/imported | ✅ | CLI/API/MCP tests; fresh clone |
| 4 | Local authorized video ingested | ✅ | Rights basis required and recorded; e2e + fresh clone |
| 5 | Video transcribed | ✅ | faster-whisper; benchmark WER < 1% |
| 6 | Word-level timestamps | ✅ | Stored per word (w, s, e, p, spk); turn onset error p50 62–85 ms |
| 7 | Speaker information | ✅ (with limitation 2) | Local diarizer; 1- and 2-speaker fixtures correct |
| 8 | Scene/visual analysis | ✅ | 29/29 scene cuts; faces; layout hints |
| 9 | ≥ 10 candidates from a long source | ✅ | 15–18 per 3-minute fixture |
| 10 | Structured scoring | ✅ | Nine factors + explanations + prior + confidence + EV |
| 11 | Compliance evaluated independently | ✅ | `compliance.evaluate` per stage; 17/17 benchmark cases |
| 12 | Top candidates rendered | ✅ | `render_top`; 12 benchmark renders |
| 13 | Vertical reframing | ✅ | 1080×1920; track/split/blur per scene |
| 14 | Subject/speaker tracking on test footage | ✅ | Tracked crop on a face 100%; split centre error 1.5% of width |
| 15 | Animated captions | ✅ | Word-highlight themes; frames inspected; captions over speech ≥ 91.7% |
| 16 | Audio in sync | ✅ | A/V drift 8–15 ms; first caption −225…+138 ms from speech |
| 17 | Reviewer plays clips in dashboard | ✅ | Browser: playback + seeking via signed range requests (local + Docker) |
| 18 | Approve/reject | ✅ | Dashboard keyboard, CLI, API, MCP |
| 19 | Metadata generated | ✅ | Per-platform copy with enforced hashtags |
| 20 | Approved clips exported | ✅ | mp4, jpg, srt, ass, json (fresh clone) |
| 21 | A publishing adapter works / fully implemented with verified mock | ✅ | Local export works end to end; YouTube/TikTok/Instagram/Upload-Post against mocks |
| 22 | Post records stored | ✅ | `posts` with idempotency keys, features snapshot |
| 23 | Metrics synced or exercised via harness | ✅ | Adapter metrics against mocks; manual/CSV import; scheduler sync |
| 24 | Earnings calculated | ✅ | Threshold, CPM, cap, budget, window; $24.00 check |
| 25 | Historical performance analyzed | ✅ | Cohorts, calibration, regression |
| 26 | Actionable observations | ✅ | Emitted only when the 80% interval excludes no effect; tested |
| 27 | CLI works | ✅ | `test_cli_workflow`; fresh clone; clean error handling |
| 28 | MCP server works | ✅ | `test_mcp_workflow_over_stdio` (real stdio server) |
| 29 | API works | ✅ | `test_api_workflow`; Docker smoke test |
| 30 | Core workflows have integration tests | ✅ | `test_e2e.py` (9), `test_postgres_s3.py` (5) |
| 31 | Docker builds pass | ✅ | `ezra`, `ezra-web` and `ezra-test` images built; stack healthy |
| 32 | Type checking passes | ✅ | `mypy` (check_untyped_defs): no issues in 57 files; `tsc --noEmit` clean |
| 33 | Lint passes | ✅ | `ruff check src tests scripts`; `eslint --max-warnings 0` |
| 34 | Automated tests pass | ✅ | Local (macOS): 70 passed, 5 skipped (Postgres-only); Docker (Linux, Postgres + MinIO): 75 passed |
| 35 | OSS licenses documented | ✅ | docs/OSS-LICENSES.md (versions from the lockfiles; OpenShorts and pyannote terms checked upstream) |
| 36 | Setup docs accurate | ✅ | README, DEPLOYMENT, `.env.example` followed verbatim in a fresh clone and on Docker |
| 37 | No critical TODO placeholders | ✅ | No TODO/FIXME/NotImplementedError in `src/`, `apps/web`, `scripts` |
| 38 | Fresh developer can follow README | ✅ | Fresh `git clone` → quick start → demo run, approve, publish, report, export |
| 39 | Demo campaign runs end to end | ✅ | `ezra run demo` in the fresh clone (README shows the real output); Docker smoke test |
| 40 | Final audit performed | ✅ | This table, after re-running the suites, benchmark, Docker stack and fresh-clone flow on the final code |

## How to check any of this yourself

```bash
uv sync --extra local && uv run pytest -q
uv run ruff check src tests scripts && uv run mypy
uv run ezra benchmark --fixtures podcast
docker compose up -d --build && uv run python scripts/smoke_api.py --api http://localhost:8000 \
  --token "$EZRA_API_TOKEN" --video ~/.cache/ezra/fixtures/podcast.mp4
```
