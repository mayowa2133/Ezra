# Campaigns

A campaign is a performance-paid programme: what it pays, what it requires and what it forbids.
Ezra imports campaigns from files you provide (it never scrapes marketplaces), normalizes them into
fields, and derives **enforceable rules**, each with a severity:

- `fail`: a violation blocks the clip (FAIL: never rendered by `render_top`, never approvable, never
  published);
- `review`: a human must look (REVIEW_REQUIRED: shown on the review card with the reason);
- `info`: recorded, shown, never blocks.

```bash
ezra campaign import campaigns/demo-campaign.yaml      # YAML, JSON or CSV; many campaigns per file
ezra campaign show demo                                # normalized fields + every rule
```

API: `POST /api/campaigns/import {text}`; MCP: `ezra_create_campaign(definition)`. Importing
again with the same `id` updates the campaign and re-derives its rules; rules you added by hand
(`origin: manual`) are kept.

## Format (YAML)

```yaml
campaign:
  id: demo                        # slug; derived from name if omitted
  name: Founder Stories (demo)
  provider: Example clipping programme
  brief: >                        # what the campaign wants; feeds the campaign-fit score
    Founder stories with real stakes: money lost, near-quitting, hard lessons.
  source_authorization: campaign_supplied   # default rights basis for footage in this campaign

  # payout
  payout_type: cpm
  cpm: 2.00                       # per 1,000 qualified views
  minimum_views: 5000             # below this a post earns nothing
  maximum_payout: 1000            # cap per clip
  budget: 5000                    # campaign-wide cap
  currency: USD
  tracking_window_days: 30        # views after this don't count
  starts_at: 2026-10-01T00:00:00Z # optional posting window
  ends_at: 2026-12-31T23:59:59Z

  # requirements
  platforms: [tiktok, instagram, youtube]
  min_duration: 15
  max_duration: 60
  hashtags: ["#founderstories"]   # required in every caption ("#" added if missing)
  mentions: ["@example"]          # required mentions ("@" added if missing)
  cta: "Follow for part 2"        # required call to action
  subtitles_required: true
  logo_required: false
  allowed_speakers: []            # if set, clips may only feature these speakers

  # prohibitions
  forbidden: [profanity, competitor mentions]   # free text, recognized phrases become rules
  forbidden_words: [crypto]
  forbidden_topics: [politics]
  competitors: [Acme Ventures]
  geography: [US, CA]             # recorded as info (Ezra can't verify audience geography)
  posting_limits: {per_day: 6, per_platform_per_day: {tiktok: 3, instagram: 3, youtube: 3}}

  human_approval_required: true   # false only matters with EZRA_ALLOW_AUTONOMOUS=1
  rules:                          # anything else, as free text → "freeform" rules (review)
    - Do not present financial advice as a guarantee.
  weights: {hook: 0.3, retention: 0.2}   # optional scoring weights (renormalized)
  brand_kit: acme                 # optional brand kit name
  source: [footage/episode-142.mp4]     # optional files to ingest on import (CLI)
```

Accepted aliases:

- the nested shape `rate: {cpm}`, `payout: {cpm, minimum_views, maximum}` and
  `requirements: {min_duration, max_duration}`;
- `minimum_qualified_views` / `min_views`, `max_payout` / `maximum_payout_per_clip`;
- `required_hashtags` / `required_mentions` / `required_cta`, `allowed_platforms`, `slug`,
  `sources`;
- a `required:` free-text list (subtitle/logo items set the flags, the rest become rules).

JSON takes the same keys. **CSV** has one campaign per row, with the same column names; list columns
(`platforms`, `hashtags`, `mentions`, `competitors`, `forbidden_topics`, `forbidden_words`, `rules`, …)
are separated by `;` or `|`.

## Rules derived from the fields

| Rule kind | From | Severity | Checked at |
|---|---|---|---|
| `duration` | min/max_duration | fail | candidate |
| `source_rights` | always | fail if rejected, review if unverified | candidate |
| `platforms` | platforms | fail | publish |
| `profanity` | `forbidden: [profanity]` | fail | candidate, publish (copy) |
| `forbidden_words` | forbidden_words | fail | candidate, publish |
| `competitors` | competitors (or "competitor mentions" without names → review) | fail | candidate, publish |
| `forbidden_topic` | forbidden_topics, and other free-text `forbidden` items | review | candidate |
| `hashtags` / `mentions` | hashtags / mentions | fail | publish (copy) |
| `cta` | cta | review | publish (copy) |
| `subtitles` / `logo` | *_required | fail | render |
| `speakers` | allowed_speakers | review | candidate |
| `window` | starts_at / ends_at | fail | publish |
| `posting_limits` | posting_limits | fail | publish |
| `geography` | geography | info | all |
| `freeform` | rules | review | candidate |

Add a rule later with `POST /api/campaigns/{ref}/rules` (kind, params, severity, description).

## How checks work (`compliance.py`)

- **Deterministic checks** (duration, words, competitors, hashtags, platforms, window, limits,
  subtitles, logo) are exact; competitor and word matching is case-insensitive on word boundaries.
- **Topics and free-form rules** can't be verified by keyword. Without a model or agent verdict
  they are REVIEW_REQUIRED with "not machine-verifiable, needs a human check". A keyword hit says
  which word matched.
- The LLM critic's per-rule verdicts (`rule_checks`, when `EZRA_LLM` is set) replace the keyword
  check. "Compliant" passes the rule; "not compliant" applies the rule's severity, with the
  model's reason on the card.
- An agent's `ezra_score_candidate(compliant=false, compliance_notes=…)` adds a review reason
  (PASS becomes REVIEW_REQUIRED). An agent can raise a concern but can't clear one: a person
  resolves REVIEW_REQUIRED at approval (the acknowledgement is audited).
- Each check returns `{rule_id, kind, outcome, message, stage}`. The candidate's
  `compliance_status` is the worst outcome: FAIL > REVIEW_REQUIRED > PASS.
- Stages: **candidate** (transcript + source), **render** (render spec), **publish** (final
  copy, platforms, schedule, today's post counts). A clip can pass at candidate stage and still
  be blocked at publish, e.g. when the copy lost a required hashtag.

## Money

For each post, `economics.payout`:

1. counts views inside the tracking window;
2. pays nothing below `minimum_views`;
3. pays `views / 1000 × CPM` above it;
4. caps at `maximum_payout`;
5. caps again at the remaining `budget` across the campaign.

`ezra earnings` shows estimated (from views) vs confirmed (revenue records you enter) and the
processing cost from `cost_records`.

Expected value per candidate is a Monte Carlo over a log-normal view distribution:

- It is fit to this account's published views when there are some, shrunk toward a prior.
- It is run through the same payout function.
- It is shown as EV per post, p10–p90, P(qualify) and its basis, and labelled an estimate.
