# Ezra: product requirements

## Problem

Performance-paid clipping programmes (Content Rewards, Whop "clipping" campaigns, brand CPM deals) pay
per qualified view: typically $1–$5 per 1,000 views, only above a minimum view count, capped per
clip and bounded by a campaign budget, with rules on duration, platforms, hashtags, competitors
and topics. The work is repetitive (watch hours of footage, find 20–60 s moments, reframe to
vertical, caption, post to three platforms, track views, claim payouts), and most clips earn
nothing because they never cross the qualification threshold.

Clipping tools (OpusClip, quso.ai, reap) optimise for "viral-looking" output. They don't know a
campaign's rules or its payout curve, and they don't learn from what a particular creator's
clips actually earned.

## Users

- **Clipper / small agency**: runs several campaigns, wants volume without breaking rules or
  spending hours in editors. Main loop: import campaign → drop footage → approve → post → get paid.
- **Creator or brand running their own campaign**: wants consistent, on-brand clips from their
  long-form content and a clear read on what works.
- **An AI agent acting for either** (Claude Code, Codex): needs the whole system reachable through
  tools, with the human decisions (approve, publish) kept human.

## Goals

1. Turn a long-form source into ranked, compliant, render-ready candidates in minutes, with a
   written reason for every score.
2. Make rules enforceable, not advisory: every campaign rule is checked at the right stage and
   produces PASS / FAIL / REVIEW_REQUIRED with a reason.
3. Rank by expected **earnings**, not only content quality: payout threshold, cap, budget and
   the creator's own history are part of the ranking and shown to the reviewer.
4. Keep a human in control of approval and publishing, and make review fast (keyboard queue,
   everything needed on one card).
5. Learn from results: sync metrics, attribute views to clip traits, report calibration, suggest
   weight changes, support A/B experiments.
6. Be operable by agents: CLI, REST API and MCP expose the same services.
7. Run locally for one person (SQLite, local files) and scale to a small team on Docker (Postgres,
   S3, separate workers) without code changes.

## Non-goals

- Scraping marketplaces or auto-joining campaigns. Campaigns are imported from files you provide.
- Posting through password automation or browser bots. Only official APIs, an aggregator you hold an
  account with, or local export.
- Clipping footage you have no right to use. Sources carry a rights basis, and unverified sources
  cannot silently flow to publication.
- Guaranteeing virality. Scores and expected values are estimates, labelled with their basis and
  uncertainty.

## Scope (v1)

| Area | In scope |
|---|---|
| Campaigns | YAML/JSON/CSV import, normalized fields, derived rules with severity, manual rules, brand kits |
| Sources | Upload, server-side import from allowed roots, URL via yt-dlp, rights status, dedupe |
| Analysis | Word-level ASR, diarization, scenes, faces, silence, topics, text signals, cached by version |
| Candidates | Heuristic windows + agent/LLM-proposed moments, nine factors with explanations, diversity, compliance, EV |
| Rendering | Aspect presets, per-scene reframing, captions (6 themes), silence/filler removal, overlays, B-roll, loudness, sidecars, platform variants |
| Review | CLI + dashboard queue, approve/reject/notes, copy editing |
| Publishing | YouTube, TikTok, Instagram official APIs; Upload-Post; local export; scheduling; limits; idempotency |
| Analytics | Metrics sync/import, earnings vs caps/budget, cohorts, calibration, regression, experiments, optimizer |
| Ops | Job queue with retries, cancel, stale recovery; audit log; cost records; Docker deployment |
| Live | Rolling-window clipping of a live stream (or a file replayed as one) into the review queue |

## Success measures

Measured by `ezra benchmark` (see BENCHMARK_REPORT.md) and by campaign reports:

- Candidates open on a sentence start and end on a sentence end (target ≥ 95%).
- Strong moments (author-labelled on the fixtures) open at least half of the top 5.
- Renders: 1080×1920 H.264/AAC, A/V drift < 100 ms, loudness −14 ± 2 LUFS, captions over speech ≥ 90%,
  no silence > 1 s left after silence removal.
- Compliance: every benchmark case gets the expected verdict for the expected reason.
- In production: share of posts that qualify for payout, and rank-vs-views Spearman ≥ 0.4 after
  ~20 posts (reported by `ezra insights`).
