# Ezra

Ezra turns long-form footage you are authorized to use into short vertical clips for performance-paid
campaigns (CPM programmes such as Content Rewards), keeps a human in charge of what gets posted, and
learns from what the posts earn.

It is agent-native: every capability is a service exposed through a **CLI**, a **REST API**, an
**MCP server** (so Claude Code, Codex or any MCP client can operate it) and a **web dashboard**. The
judgement parts (finding moments, scoring them, writing copy) can be done by Ezra's own heuristics,
by an agent over MCP, or by a local model CLI. Ezra never calls a paid model API on its own.

```
ezra run demo
```

Real output on the 3-minute synthetic interview that `ezra benchmark` generates (fresh clone, Apple M-series,
about 2.5 minutes including transcription, analysis and five renders):

```
1 source(s) · 16 candidates (16 publishable) · rendered 5 · review queue 5
                                          ready for review                                          
┏━━━━━━┳━━━━━━┳━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ clip ┃ rank ┃ dur ┃ layout ┃ compliance      ┃ EV/post ┃ opens with                              ┃
┡━━━━━━╇━━━━━━╇━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 1    │ 74.3 │ 20s │ split  │ REVIEW_REQUIRED │ $24.00  │ "I think most startups should never     │
│      │      │     │        │                 │         │ raise venture money.…"                  │
│ 2    │ 72.6 │ 32s │ split  │ REVIEW_REQUIRED │ $23.21  │ "Today my guest built a company, lost   │
│      │      │     │        │                 │         │ almost everything, and…"                │
│ 3    │ 72.2 │ 25s │ split  │ REVIEW_REQUIRED │ $23.05  │ "We raised $2 million before we had a   │
│      │      │     │        │                 │         │ single paying…"                         │
│ 4    │ 69.3 │ 17s │ split  │ REVIEW_REQUIRED │ $21.83  │ "Was there a moment you almost quit? I  │
│      │      │     │        │                 │         │ almost quit the…"                       │
│ 5    │ 67.1 │ 49s │ split  │ REVIEW_REQUIRED │ $20.88  │ "My co-founder read it, tore it in      │
│      │      │     │        │                 │         │ half, and said give me"                 │
└──────┴──────┴─────┴────────┴─────────────────┴─────────┴─────────────────────────────────────────┘
next: `ezra review` (or the dashboard /review)
```

Every clip is REVIEW_REQUIRED because the demo campaign forbids a topic (politics) and has a free-form
rule, which only a person can clear. That is the approval gate doing its job.

## What it does

| Stage | What happens |
|---|---|
| Campaigns | Import YAML/JSON/CSV rules (CPM, qualified-view threshold, caps, budget, platforms, duration, hashtags, forbidden topics, competitors, posting limits). Each becomes an enforceable rule with a severity. |
| Sources | Ingest a file or URL with a **rights basis**; unverified footage is flagged on every candidate. SHA-256 dedupe, ffprobe validation. |
| Analysis | faster-whisper word timestamps, speaker diarization, scene cuts, face tracks, silence, topic segments, emotion/Q&A signals, each stored with provider, version and confidence. |
| Candidates | Sentence-aligned windows scored on nine factors (hook, retention, context, emotion, novelty, discussion, payoff, visual, campaign fit), each with a written reason; diversity-filtered; compliance checked; expected value estimated from the campaign's payout rules. |
| Ranking | Campaign weights + a performance prior learned from your own published results + optional model/agent critique. |
| Rendering | 9:16 / 1:1 / 16:9 / 4:5; per-scene layout (face tracking, split screen for two-shots, blurred fit for slides); silence and filler removal with waveform-refined cuts; six animated caption themes; hook card, logo, watermark, CTA, punch-ins, B-roll, loudness normalization; SRT/ASS sidecars. |
| Review | Keyboard-driven queue (CLI and dashboard) with scores, reasons, compliance and EV side by side. Nothing publishes without approval. |
| Publishing | Official APIs for YouTube, TikTok and Instagram (OAuth + PKCE, resumable uploads), optional Upload-Post, and a local export target. Dry run first, idempotency keys, posting limits, scheduling by timezone. |
| Learning | Metrics sync, qualified-view earnings against caps and budget, trait cohorts with shrinkage, rank-vs-views calibration, A/B experiments, weight optimization. |

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), ffmpeg/ffprobe on PATH. Node 22 for the
dashboard. Docker alternative: see [Deployment](docs/DEPLOYMENT.md).

```bash
uv sync --extra local                   # deps + faster-whisper, OpenCV, PySceneDetect
uv run ezra init                        # creates ./data (SQLite + local storage)
uv run ezra campaign import campaigns/demo-campaign.yaml
uv run ezra source add ~/footage/episode.mp4 --campaign demo --rights campaign_supplied
uv run ezra analyze --source 1
uv run ezra candidates --campaign demo --top 10
uv run ezra render --campaign demo --top 5
uv run ezra review --campaign demo      # a = approve, r = reject, s = skip
uv run ezra accounts add tiktok local-export me
uv run ezra publish --clip 1 --platforms tiktok      # shows the dry run, asks before posting
uv run ezra metrics add 1 12000                      # or: ezra metrics sync (platform APIs)
uv run ezra report demo
```

No footage handy? `uv run ezra benchmark --fixtures podcast` synthesizes a 3-minute two-speaker
interview (macOS `say` or espeak-ng), runs the whole pipeline on it, and leaves the video at
`~/.cache/ezra/fixtures/podcast.mp4`, ready for `ezra source add`.

Dashboard and API:

```bash
uv run ezra serve                       # API on :8000 with an embedded worker (OpenAPI at /docs)
cd apps/web && npm install && npm run dev   # dashboard on :3000
```

With an agent: this repo ships `.mcp.json`, so Claude Code picks up the `ezra` MCP server; the
operating guide is [skills/ezra/SKILL.md](skills/ezra/SKILL.md). Codex reads MCP servers from
`~/.codex/config.toml`:

```toml
[mcp_servers.ezra]
command = "uv"
args = ["run", "--directory", "/path/to/Ezra", "ezra", "mcp"]
```

## Documentation

| | |
|---|---|
| [PRD](docs/PRD.md) | Problem, users, scope, success measures |
| [Architecture](docs/ARCHITECTURE.md) | Services, job queue, providers, rendering pipeline |
| [Data model](docs/DATA-MODEL.md) | Tables and lifecycles |
| [API](docs/API.md) · [MCP](docs/MCP.md) · [CLI](docs/CLI.md) | Interfaces |
| [Campaigns](docs/CAMPAIGNS.md) | Campaign file format and how rules are enforced |
| [Deployment](docs/DEPLOYMENT.md) | Local, Docker Compose (Postgres + MinIO), configuration |
| [Security](docs/SECURITY.md) | Secrets, auth, uploads, OAuth, content rights |
| [Testing](docs/TESTING.md) · [Benchmark](BENCHMARK_REPORT.md) | How it is verified and how well it does |
| [Decisions](docs/DECISIONS.md) · [OSS licenses](docs/OSS-LICENSES.md) | Why it is built this way; what it uses |
| [Build report](BUILD_REPORT.md) · [External setup](REMAINING_EXTERNAL_SETUP.md) | Status and what needs your accounts |

## Ground rules Ezra enforces

- Footage needs a rights basis; clips from unverified sources are marked REVIEW_REQUIRED, rejected
  sources FAIL.
- A human approves every clip, and publishing is a dry run until confirmed. Autonomous publishing
  needs both the campaign flag and `EZRA_ALLOW_AUTONOMOUS=1`, and still only posts PASS clips.
- Publishing uses official APIs or your own export; no password automation, no scraping of
  marketplaces.
- Secrets live in the environment or an encrypted store, never in ordinary tables.
