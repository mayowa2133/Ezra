# Ezra: agent guide

Ezra is an agent-native clipping and performance-optimization system: campaign rules and authorized
footage in, human-approved short clips out, results fed back into ranking. One set of services
(`src/ezra/`) behind four interfaces: CLI (`ezra`), REST API (FastAPI), MCP server (`ezra mcp`) and
a Next.js dashboard (`apps/web`).

**Model calls are optional and never go to a paid API by default.** Judgement is done by Ezra's
heuristics, by the agent driving the MCP server, or by a local CLI provider (`EZRA_LLM=claude-cli |
codex-cli | openai-compatible`). Don't add a direct Anthropic/OpenAI SDK dependency.

## Map

| Path | Owns |
|---|---|
| `config.py` | `Settings` (all `EZRA_*` env vars); `reset_settings()` for tests |
| `db/` | SQLAlchemy models, Alembic migrations (`db/migrations/versions`), `session()`, `migrate()` |
| `storage.py` | `LocalStorage` / `S3Storage`, key validation (no traversal), `local_path()` for media tools |
| `jobs.py`, `worker.py`, `tasks.py` | DB-backed queue (SKIP LOCKED on Postgres), retries/backoff, cancel, stale requeue; each job in a child process |
| `secrets.py` | Env + Fernet-encrypted store; tables hold only a `credential_ref` |
| `security.py`, `audit.py` | Upload validation, import roots, OAuth state/PKCE, webhook signing, rate limiting, audit log |
| `campaigns.py`, `compliance.py` | Campaign import (YAML/JSON/CSV) → normalized fields + rules; stage checks → PASS / FAIL / REVIEW_REQUIRED |
| `sources.py` | Ingest with rights basis, sha256 dedupe, ffprobe |
| `transcription/`, `diarization.py` | faster-whisper / WhisperX, sentence resegmentation, **`snap()`** word-edge cuts; local MFCC or pyannote diarization |
| `analysis/` | Scenes (PySceneDetect), faces (Haar / MediaPipe), silence, topics, text signals; stored per product with provider+version |
| `scoring/`, `candidates.py` | Features, nine-factor heuristic, windows, diversity shortlist, agent/LLM critique, ranking |
| `economics.py`, `analytics.py`, `experiments.py`, `runner.py` | Payout math, EV, cohorts/calibration/regression, experiments, `run_campaign`, weight optimization |
| `render/` | `spec.py` (RenderSpec, platform presets), `edl.py` (silence/filler cuts, waveform refinement), `layout.py` (per-scene reframing), `captions.py` (themes, SRT/ASS), `compose.py` (ffmpeg graph) |
| `review.py`, `metadata.py` | Approval gate; per-platform copy with required tags enforced |
| `publishing/` | Publisher adapters (YouTube, TikTok, Instagram, Upload-Post, local-export), OAuth, scheduling |
| `metrics.py`, `broll.py`, `live.py`, `costs.py` | Metrics sync/import, B-roll, live clipping, cost records |
| `api/app.py`, `cli.py`, `mcp_server.py` | Interfaces. `mcp_server.INSTRUCTIONS` is the canonical agent workflow |
| `benchmark/` | Synthetic fixtures with ground truth, `run.py` quality benchmark |
| `apps/web/` | Dashboard; talks to the API through a server-side proxy that holds the token |

## Invariants: don't break these

- **Human approval.** `review.approve` is the only way to `approved`; publishing refuses anything
  else, and `publish_clip` is a dry run unless `confirm=True`. Autonomous publish needs the
  campaign's `human_approval_required: false` *and* `EZRA_ALLOW_AUTONOMOUS=1`, and only PASS clips.
- **Compliance is evaluated independently of scoring** (`compliance.evaluate`), per stage. FAIL
  candidates are never rendered by `render_top` and can't be approved.
- **Weights are applied by Ezra, not by agents or models.** Agents score factors 0-100
  (`candidates.set_agent_scores`); `heuristic.content_score` applies campaign weights. This keeps
  scores comparable and the calibration check meaningful.
- **Cut boundaries go through `transcription.base.snap`** (word edges) and, at render time,
  `edl.refine_boundaries` (quietest audio point). Never cut on raw timestamps.
- **Secrets never touch ordinary tables** — store a ref, put the value in `secrets.put`.
- **Every DB access opens its own session** (`db.session()`); the API, MCP server and worker are
  threaded/multi-process.
- **Media keys go through `storage.validate_key`**; never join user input onto filesystem paths.
- Schema changes need an Alembic migration: edit `db/models.py`, run
  `uv run ezra db revision -m "what changed"`, review the file. It must work on SQLite and PostgreSQL
  (`tests/test_core.py::test_migrations_match_the_models` catches drift).

## Working here

```bash
uv sync --extra local                 # deps + faster-whisper / OpenCV / PySceneDetect
uv run pytest -q                      # ~4 min; renders real video, builds TTS fixtures (cached)
uv run ruff check src tests scripts && uv run mypy
uv run ezra --help
uv run ezra serve                     # API + embedded worker on :8000
uv run ezra mcp                       # MCP server on stdio
cd apps/web && npm run lint && npm run typecheck && npm run build
docker compose --profile test run --rm test   # suite against Postgres + MinIO
uv run ezra benchmark --fixtures podcast      # quality regression check
```

Tests use synthetic fixtures (`benchmark/fixtures.py`, TTS + a public-domain portrait) cached in
`~/.cache/ezra/fixtures`; `tests/test_e2e.py` drives the pipeline, the API, the CLI and the MCP server
over stdio. Add a test with every behaviour change; run the benchmark when you touch scoring,
candidates, layout, captions or the EDL. Commit in small, working slices.
