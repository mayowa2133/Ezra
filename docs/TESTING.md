# Testing

```bash
uv run pytest -q                                   # 67 tests + 5 Postgres/S3 tests skipped locally, ~2 min
uv run ruff check src tests scripts                # lint (config in pyproject.toml)
uv run mypy                                        # type check (check_untyped_defs, pydantic plugin)
cd apps/web && npm run lint && npm run typecheck && npm run build
docker compose --profile test run --rm test        # the whole suite on Linux against Postgres + MinIO (72 tests)
uv run ezra benchmark                              # quality benchmark (~5 min); see BENCHMARK_REPORT.md
uv run python scripts/smoke_api.py --api URL --token T --video file.mp4   # end-to-end against a running API
```

## Principles

- **Real media, no network.** Tests render real video with ffmpeg and run real faster-whisper
  (`base` model), OpenCV and PySceneDetect on synthetic footage. Platform APIs are exercised
  against `httpx.MockTransport` handlers that follow the documented request/response shapes, and
  OAuth, S3 (moto) and OpenShorts are mocked at the HTTP boundary. Nothing posts anywhere.
- **Legally clean fixtures.** `ezra.benchmark.fixtures` builds each fixture at test time:
  - speech from the local TTS engine (macOS `say`, or espeak-ng on Linux/Docker);
  - a public-domain NASA portrait for faces;
  - ground truth (speaker turns, scene cuts, face boxes, long pauses, fillers) written alongside.

  Fixtures are cached in `~/.cache/ezra/fixtures` (`EZRA_FIXTURE_CACHE`), models in
  `~/.cache/ezra/models`. Tests needing TTS skip, with a reason, when neither engine exists.
- **Isolation.** Every test gets its own `EZRA_HOME` (SQLite + local storage) with provider env vars
  cleared. The expensive "processed podcast" state (analysis + candidates) is built once per
  session and copied into each test that needs it (`use_processed`).

## What is covered

| File | Covers |
|---|---|
| `test_core.py` | Migrations (and **models = migrations** via autogenerate compare), job lifecycle, progress/logs, retry with backoff, permanent failures, cancel, dedupe, stale requeue, the isolated worker (child process + cancellation), local storage + traversal guard, S3 storage (moto), secret store |
| `test_campaigns_compliance.py` | YAML nested/flat, JSON, CSV import; validation errors; re-import keeps manual rules; rule derivation; candidate/render/publish-stage checks; rights and review rules |
| `test_render_units.py` | Word joining and sentence resegmentation, `snap`, EDL (fillers, long pauses, remapping), **waveform cut refinement**, caption chunking/themes/SRT/ASS, emoji stripping, layout modes, shot planning; a real split-layout render checked by pixel colour |
| `test_scoring.py` | Interviewer detection, windows never ending on the interviewer's segue, topic-drift penalties |
| `test_economics_analytics.py` | Payout (threshold, cap, budget, tracking window), confirmed vs estimated, EV monotonic and labelled, cohorts/calibration/prior, learnings, experiment winner only with evidence, optimizer needing data |
| `test_security_diarization.py` | Media validation, import roots, webhook signatures, OAuth state (single-use, provider-bound, expiring), safe redirects, rate limiter, MFCC + clustering on synthetic voices, **one-speaker default**, speaker assignment |
| `test_publishers.py` | YouTube (OAuth + PKCE, resumable upload, `publishAt`, metrics, token refresh), TikTok (FILE_UPLOAD init, single and multi-chunk `Content-Range`, SELF_ONLY for unaudited apps, status, failure, video query), Instagram (REELS container, rupload, polling, publish, insights, no private posts), 429 → retryable, missing credentials name the env var, local export |
| `test_e2e.py` | The **pipeline on real footage** (transcript, 2 speakers, faces/layouts, topics, ≥10 scored candidates with compliance and EV, a 1080×1920 split render with A/V drift < 0.12 s and first caption within 0.4 s of speech, approve → metadata → dry run → local-export publish → duplicate refusal → metrics → earnings → insights → export); platform variant + re-render; the **API** (auth, 404s, render job, signed ranged media, tampered signature, approve, publish problems, bad upload type, earnings, dashboard); the **CLI**; the **MCP server over stdio** (tool list, transcript paging, agent scoring, render job polling, approve, publish dry run); **OpenShorts** import |
| `test_postgres_s3.py` | Postgres migrations, `SKIP LOCKED` claiming with 6 concurrent workers, campaign/source/job on Postgres + MinIO, S3 key validation, and the full analyze → candidates → render → approve pipeline on the production backends |

## Verified outside the automated suite

- `docker compose up` stack: Postgres, MinIO, migrate, api, worker, web.
  `scripts/smoke_api.py` passed end to end against it: upload → analysis → candidates → render →
  ranged media from MinIO → approve → local-export publish → metrics → earnings.
- The dashboard was checked in a browser against the local API: every page loads, the proxied API
  routes return 200, keyboard review approves and advances, and video plays and seeks through signed
  range requests. Against the Docker stack, all 12 pages and the proxied API routes return 200 with
  no console errors, and the review page plays a clip served from MinIO.
- `ezra benchmark` on four fixtures (see BENCHMARK_REPORT.md).

## Not covered (needs external accounts)

- Real posting to YouTube, TikTok and Instagram, and real metrics pulls. The adapters are tested
  against mocked APIs only (see REMAINING_EXTERNAL_SETUP.md).
- pyannote and WhisperX providers (optional heavy installs, pyannote needs `HF_TOKEN`).
- A live OpenShorts backend (mocked at its REST API).
- `EZRA_LLM=codex-cli` and `openai-compatible` against real models. (`claude-cli` was verified
  live: one structured `claude -p --json-schema` call returned schema-valid output with usage
  accounted.)

## Adding tests

- Add a test with every behaviour change, and prefer the real path (real ffmpeg, real DB) with the
  network mocked at the HTTP boundary.
- Media tests should use `fixture_video(name)` or `tiny_video`, never downloaded or committed media.
- When changing scoring, candidates, layout, captions or the EDL, run `ezra benchmark` and compare
  its report with the last one.
