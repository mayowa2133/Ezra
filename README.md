# clipper

A local clipping operator for campaigns like Content Rewards. Give it long-form footage and a campaign's rules.
Your coding agent (Claude Code or Codex) finds and ranks the moments, you approve the good ones, and clipper
posts them and tracks what they earn. What performs feeds back into how the agent judges the next batch.

```
clipper run campaign-184
```

```
Campaign: Podcast XYZ
CPM: $2.00
Source: episode-142

Analyzed: 01:14:03.0
Candidate moments: 37
Rendered: 5

Top clips:

1. "He lost $400k overnight"
   AI score: 94/100
   Duration: 31s
   Opens on the loss itself; lesson lands at ~20s and pays it off.

2. "Nobody tells founders this"
   AI score: 91/100
   ...

Approve clips? [1,2,3,4,5] 1,2

Generating platform copy...

Clip 12
  Tiktok: He lost $400k overnight 😳 #podcastxyz
  Instagram: One mistake almost cost him everything... #podcastxyz
  Youtube: How He Lost $400,000 Overnight

Publish now? [y/N] y
✓ Tiktok published
✓ Instagram published
✓ Youtube published

Tracking enabled.
```

## How it works

```
 campaign.yaml ──┐
 episode.mp4 ────┤
                 ▼
        clipper (local, deterministic)                 your agent over MCP (the judgment)
 ┌──────────────────────────────────────┐        ┌──────────────────────────────────────┐
 │ faster-whisper transcript (words)    │ ─────▶ │ reads the whole transcript            │
 │                                      │ ◀───── │ proposes ~20 moments                  │
 │ snap to word edges, compliance check │ ─────▶ │ scores each on the rubric,            │
 │ weight scores → ai_score             │ ◀───── │ comparing them against each other     │
 │ render top N: 9:16 crop that follows │        │                                      │
 │ the speaker's face, burned captions  │        │                                      │
 │ ─── you approve in the terminal ───  │        │                                      │
 │ hashtag / rule gate on copy          │ ◀───── │ writes per-platform copy              │
 │ ─── you confirm publishing ───       │        │                                      │
 │ Upload-Post → TikTok / IG / YouTube  │        │                                      │
 │ metrics → revenue → insights         │ ─────▶ │ turns stats into saved learnings,     │
 └──────────────────────────────────────┘        │ which go into every future brief      │
                                                 └──────────────────────────────────────┘
```

- **No model API.** clipper never calls an LLM. The thinking happens in Claude Code or Codex through the
  `clipper` MCP server, on your existing subscription. `clipper run` starts headless `claude -p` (or
  `codex exec`) sessions for the judgment steps.
- **The agent judges, code keeps it honest.** The agent scores seven dimensions and clipper applies the
  weights, so scores stay comparable. Cut points snap to word boundaries. Duration, profanity, competitor
  and forbidden-term rules are enforced in code before the judge sees a clip, and required hashtags are
  enforced before anything posts.
- **Nothing ships without you.** Headless sessions can't approve or publish. Publishing needs a human
  approval plus an explicit confirm.

Default rubric (override per campaign):

| Hook | Retention | Standalone context | Emotion | Novelty | Comment potential | Campaign fit |
|---|---|---|---|---|---|---|
| 25% | 20% | 15% | 10% | 10% | 10% | 10% |

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), ffmpeg, and Claude Code (`claude`) or Codex (`codex`) signed in.

```bash
uv sync --extra local                 # clipper + faster-whisper + OpenCV (face tracking)
cp .env.example .env                  # add Upload-Post keys when you want to publish
uv run clipper init
```

## Use it

```bash
uv run clipper campaign create campaigns/example.yaml
uv run clipper source add campaign-184 ~/footage/episode-142.mp4
uv run clipper run campaign-184       # the whole loop
```

Or do it from inside Claude Code in this repo: `.mcp.json` registers the server and the `clipping` skill
knows the workflow. Just say *"clip campaign-184"*.

Step by step, if you want control:

| Command | What it does |
|---|---|
| `clipper transcribe <source_id>` | Local Whisper transcription with word timestamps |
| `clipper run <campaign> [--candidates 20 --render 5 --agent codex --openshorts --no-publish]` | The full loop |
| `clipper clips <campaign> [--status scored]` | Ranked clips |
| `clipper render <clip_id>... [--framing blur --crop-x 0.3]` | Re-render; the crop follows the speaker unless you pin `--crop-x` |
| `clipper review <campaign>` | Approve or reject rendered clips |
| `clipper copy <campaign>` | Agent writes platform copy for approved clips |
| `clipper publish <campaign> [--dry-run --schedule 2026-10-01T18:00]` | Post approved clips (asks first) |
| `clipper metrics sync` / `clipper metrics add <post_id> <views> --payout 42` | Pull or record performance |
| `clipper report <campaign>` | Views, qualified views, estimated vs actual revenue |
| `clipper insights [--explain]` | What's working; `--explain` has the agent save learnings |
| `clipper mcp` / `clipper mcp-config [--codex]` | Run the MCP server, or print config for other clients |

Revenue follows the campaign file: `qualified = views ≥ minimum_views ? views : 0`, then
`estimate = min(qualified / 1000 × CPM, maximum_payout)`, and the campaign total is capped at `budget`.

## Campaigns

See [`campaigns/example.yaml`](campaigns/example.yaml): CPM, minimum views, payout caps, duration limits,
forbidden and required items, hashtags, platforms, a free-text brief and optional rubric weights.

## Optional: OpenShorts

[OpenShorts](https://github.com/mutonby/openshorts) (MIT) can run alongside as a second opinion and a better
renderer: active-speaker detection and split-screen layouts (clipper's own crop follows the most
prominent face, shot by shot). `clipper run --openshorts` sends each source through a
self-hosted OpenShorts and imports its moments as candidates. Your agent scores them against its own picks.
When an OpenShorts moment wins, `--framing openshorts` reuses its render. Start it with
`docker compose --profile openshorts up -d`. It needs its own picker model: a Gemini key, or a local Ollama.

## Publishing notes

Publishing uses [Upload-Post](https://upload-post.com): one API for TikTok, Instagram and YouTube. The free plan
allows 10 uploads a month, and TikTok needs a paid tier. Separately, TikTok keeps posts from unaudited API
clients private until the app passes review.

## Development

```bash
uv run pytest -q
```

See [AGENTS.md](AGENTS.md) for the module map and the invariants.
