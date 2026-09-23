# What's next for Ezra

Written 2026-09-23, after the build. Companion to [BUILD_REPORT.md](BUILD_REPORT.md) (what exists),
[BENCHMARK_REPORT.md](BENCHMARK_REPORT.md) (how well it works on synthetic footage) and
[REMAINING_EXTERNAL_SETUP.md](REMAINING_EXTERNAL_SETUP.md) (the account steps).

## 1. Where things stand

| | State |
|---|---|
| Pipeline (campaign → analysis → candidates → render → review → publish → metrics → earnings → learning) | Works end to end, verified locally, in Docker and from a fresh clone |
| Interfaces | CLI, REST API, MCP server, dashboard, all tested |
| Quality on synthetic footage | Transcription, scene cuts and renders are strong. Moment selection is the weak spot: 3 of 6 labelled strong moments open a top-5 clip. |
| Real platforms | Adapters built against the documented APIs and tested with mocks. **Never posted for real.** |
| Real footage | **Not yet tried.** Every number so far comes from text-to-speech voices and still photos. |
| Repository | 8 commits on `main`, local only (not pushed) |

The two biggest unknowns are how Ezra does on real footage, and whether the platform adapters
behave against the live APIs. Everything below is ordered to settle those first.

## 2. This week: things only you can do

These need your accounts, money or decisions.

### 2.1 Push the work
```bash
git push origin main
```
There are 8 local commits. Nothing has been pushed since the dashboard work started.

### 2.2 Pick a publishing route
| Route | Setup effort | Public posts from day one? | Best for |
|---|---|---|---|
| **Local export** (already works) | None | You upload the files yourself | Trying Ezra on real campaigns immediately |
| **Upload-Post** | An account, a profile, an API key | Yes, within their plan limits | Posting quickly without platform app reviews |
| **Official APIs** (YouTube, TikTok, Instagram) | Developer apps, consent screens, **platform audits/reviews** | **No.** YouTube and TikTok keep uploads private until your app passes their audit; Instagram needs App Review for other people's accounts | Long-term, no middleman, full metrics |

Recommendation: start with **local export or Upload-Post** while the official-app reviews run in the
background. Reviews take from days to weeks and aren't in our control. Start them now so they're
done when you need them.

### 2.3 Set the two secrets (before anyone else can reach the API)
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # run twice
```
Put one value in `EZRA_API_TOKEN` and one in `EZRA_SECRET_KEY` in `.env`. **Back up
`EZRA_SECRET_KEY`**: losing it means reconnecting every account.

### 2.4 Import one real campaign
Copy a real campaign's terms (CPM, minimum views, cap, budget, platforms, duration, hashtags,
forbidden topics, competitors) into a YAML file modelled on `campaigns/demo-campaign.yaml`, then:
```bash
uv run ezra campaign import campaigns/<name>.yaml
uv run ezra campaign show <slug>        # check every rule and severity
```
Check the derived rules carefully. A missed rule becomes a clip that doesn't get paid.

## 3. Weeks 1–2: validate on real footage (the most important step)

The benchmark proves the plumbing, but the moment picking has only been tested on synthetic speech.
Before trusting Ezra's rankings with money:

1. **Run 3–5 real episodes** you have the rights to, ideally different formats (solo talking head,
   two-person podcast, 3+ person panel, something with slides or B-roll):
   ```bash
   uv run ezra source add episode.mp4 --campaign <slug> --rights campaign_supplied
   uv run ezra run <slug> --top 8
   uv run ezra review --campaign <slug>
   ```
2. **Label what you'd actually post.** For each episode, note the 5–10 moments you would clip by
   hand (start time and a one-line reason). This becomes a private "real footage" benchmark.
3. **Compare.** How many of your moments appear in Ezra's top 10? Do the cuts start and end where
   you would? Are the captions right? Does the split layout choose the right people?
4. **Run it again with an agent judging.** In Claude Code, ask it to read the whole transcript
   through the MCP tools, propose moments, and score candidates. Compare that against the
   heuristic ranking. This is the intended way to use Ezra; the heuristics alone are a baseline.
5. **Write down every miss** (wrong speaker, bad crop, clipped word, dull clip ranked high). Those
   become the engineering backlog in section 5, re-ordered by what real footage shows.

Deliverable: a short findings note, plus a decision on whether Ezra's rankings are good enough to
act on with an agent in the loop.

## 4. Weeks 2–6: start the learning loop

Ezra learns from what your posts earn, but it needs data first:

| Posts published | What becomes available |
|---|---|
| 1–2 | Earnings, qualified-view tracking |
| ≥ 3 | The performance prior starts nudging rankings (low weight) |
| ~10 | Warnings when the rank score isn't predicting views |
| ~15 | Regression over traits |
| ~20+ | Calibration you can act on; `ezra optimize` weight suggestions become meaningful |

To get there efficiently:

- **Post variety deliberately** early on (different hook types, durations, caption themes).
  Posting only "safe" clips teaches the system nothing.
- **Run one experiment at a time**, e.g. caption theme:
  ```bash
  uv run ezra experiment create <slug> "caption theme" caption_theme --values bold,karaoke --min-samples 5
  ```
- **Keep metrics flowing**:
  - with platform APIs connected, the worker syncs every 6 h (`EZRA_METRICS_SYNC_HOURS`);
  - otherwise, enter numbers from the campaign dashboard with `ezra metrics add` or
    `ezra metrics import file.csv`;
  - record actual payouts too (`POST /api/campaigns/{ref}/revenue`), so confirmed earnings can
    be compared with estimates.
- **Review every week or two**:
  ```bash
  uv run ezra insights --campaign <slug>
  uv run ezra optimize <slug>              # suggestions only; add --apply when you agree
  ```

## 5. Engineering backlog (prioritized)

Priorities are set by expected impact on earnings. Revisit them after the real-footage findings.

### P1: Moment selection (biggest quality gap)

| Item | Why | Approach |
|---|---|---|
| **Story-level deduplication** | The top 5 can contain two cuts of the same story (#4 and #5 on the fixtures) | Penalize candidates that share their key sentence or hook with a higher-ranked one, not only time overlap |
| **Open on the strongest line** | The $400k story ranked, but opened on the host's teaser rather than the loss line itself | For each story window, generate a start variant at its highest-hook sentence (a number, stakes or first-person claim) and let scoring choose |
| **Agent judging as the default path** | Heuristic confidence is 0.35; an agent reading the whole transcript does better | A `ezra run --agent claude` mode: a headless session with the MCP server that proposes and scores, but never approves or publishes. The old clipper did this; it wasn't carried over. |
| **Model critic out of the box** | `EZRA_LLM=claude-cli` works but is opt-in | Document it in the quick start; consider it the default when `claude` is on PATH |

### P1: Publishing against real APIs
- As each platform account connects, do **one private test post**, check `ezra posts`, and
  sync metrics. Fix whatever the real API does differently from the documented shapes the mocks
  follow (field names, processing delays, quota errors).
- Add a `ezra accounts test <id>` command that checks the token and permissions without posting.

### P2: Speakers and framing
| Item | Why | Approach |
|---|---|---|
| **pyannote benchmark** | The local diarizer merged 3 speakers into 2 | Run with `HF_TOKEN`; add a benchmark mode per provider |
| **Better local voice embeddings** | Keep a no-account default that handles 3+ speakers | Evaluate a small ONNX speaker-embedding model (runs on the onnxruntime Ezra already ships) instead of MFCC statistics |
| **Active speaker detection** | In multi-person shots, the crop follows the biggest face, not the one talking | Combine per-face mouth motion with speaker turns to pick who to track |
| **MediaPipe benchmark** | Haar misses some profiles and close-ups (92% recall on close-ups) | Run the benchmark with `EZRA_FACE_DETECTOR=mediapipe`; switch the default if it wins |

### P2: Output polish
- Text-card B-roll headlines from a model when one is configured (the keyword fallback is
  readable but flat).
- Thumbnail text overlays, and thumbnail A/B experiments.
- A per-campaign "house style" preset (caption theme, colors, hook card, CTA) through brand kits,
  with a dashboard editor.

### P3: Production and team use
| Item | Why |
|---|---|
| Multi-user sign-in (OIDC) with roles and per-person audit | One API token grants full access today |
| TLS / reverse-proxy recipe (e.g. Caddy) in the compose file | Production deployments need HTTPS for OAuth callbacks |
| Database and MinIO backup job | Protect campaign history and the learning data |
| Error tracking and metrics (Sentry or OpenTelemetry) | See failed jobs and slow renders without reading logs |
| GPU Docker image | Faster transcription at volume |
| Inbound webhooks | Platform status push instead of polling (helpers exist, no routes) |
| Official marketplace APIs behind `CampaignSourceAdapter` | Only if programmes publish them; no scraping |

### P3: Later
- A learned ranking model, once there are a few hundred posts with metrics (the regression and
  calibration code is in place to evaluate it honestly).
- WhisperX forced alignment by default, if PyTorch's size becomes acceptable.

## 6. Risks and how to watch them

| Risk | Signal | Mitigation |
|---|---|---|
| Rankings don't match what earns | `ezra insights` calibration below 0.1 after ~10 posts | Agent judging; experiments; `ezra optimize` |
| A rule is missed and a payout is lost | Rejected submissions from the programme | Review `ezra campaign show` on import; add manual rules; keep human approval on |
| Platform restrictions (private-only until audit, quotas, rate limits) | Posts stuck private; 403/429 in job errors | Start audits early; Upload-Post as a bridge; posting limits in campaign files |
| Rights disputes | Takedowns, strikes | Never ingest without a rights basis; keep `campaign_supplied` evidence; unverified sources stay REVIEW_REQUIRED |
| Losing `EZRA_SECRET_KEY` | Stored tokens can't be decrypted | Back it up in a password manager |
| Over-trusting synthetic benchmark numbers | Real-footage findings differ | Section 3 real-footage benchmark |

## 7. Suggested sequence

| When | Do |
|---|---|
| Today | Push; set secrets; start YouTube/TikTok/Instagram app applications (or sign up for Upload-Post) |
| Week 1 | Import a real campaign; run 3–5 real episodes; label your picks; compare heuristic vs agent judging |
| Week 2 | Findings note → re-prioritize the backlog; build story dedupe and "open on the strongest line"; first real posts (local export or Upload-Post) |
| Weeks 2–4 | One test post per official platform as each account connects; fix real-API differences; add `ezra accounts test` |
| Weeks 3–6 | Post with variety; one experiment at a time; metrics flowing; review insights every week or two |
| After ~20 posts | Act on calibration and `ezra optimize`; decide on the P2 speaker/framing work based on where clips lose views |

## 8. Decisions needed from you

1. **Push now?** (8 commits are waiting)
2. **Publishing route for the first month**: local export, Upload-Post, or wait for the official apps?
3. **Which real footage and campaign** to validate on first?
4. **Agent judging by default?** It uses your Claude/Codex subscription for each run. It's likely
   better quality, at the cost of subscription usage and a slower run.
5. **Single user or team?** This decides whether multi-user sign-in moves up from P3.
