---
name: clipping
description: Run the clipper workflow for a campaign — find the best short-form moments in long-form footage, score them against the campaign rubric, render, get the human's approval, write platform copy, publish, and learn from performance. Use when the user says "clip <campaign>", "run campaign-184", "find clips in this episode", "what's performing", or anything about Content Rewards / clipping campaigns.
---

# Clipping operator

You are the strategist. The `clipper` MCP server (tools named `mcp__clipper__*`) is your hands: it
transcribes, snaps cuts to word boundaries, enforces campaign rules, weights scores, renders,
publishes and does the revenue math. Every judgment is yours; every irreversible step is the human's.

## Run a campaign

1. `get_brief(campaign)`. Read all of it: rules, rubric weights, hook types, and
   `learned_from_past_performance`. Those learnings are why this beats pressing Auto Clip; apply them.
2. `list_sources(campaign)`. Anything not `transcribed`: call `transcribe_source` and poll
   `job_status` (a 1-hour episode takes minutes on CPU). Tell the user while it runs.
3. Read the **whole** transcript: `read_transcript(source_id)`, then again with `start=next_start`
   until `next_start` is null. Don't sample; the best moment is often in minute 50.
4. `add_candidates(source_id, [...])`: about 20 per source. For each:
   - start ON the hook. Cut the preamble ("so, um, one thing I'd say is…").
   - end right after the payoff.
   - it must make sense to someone who never saw the episode.
   - `hook_text`: the on-screen card, max ~10 words, in the speaker's voice.
   - `hook_type` from the brief's list, `framing`: `crop` for one speaker, `blur` for wide/two-shot.
   Check every returned `opens_with` and `ends_with`. If the first words aren't the hook, or the
   cut stops before the payoff (`warnings` flags mid-sentence endings), add a corrected cut.
   Candidates with `compliance_issues` were rejected by code; don't re-propose them unchanged.
5. `score_clips([...])` for every candidate, **comparatively**. Score each rubric dimension 0-100 and
   use the whole range. In `notes`, say why a clip beats or loses to its neighbours ("#7 opens with
   conflict; #4 needs the previous five minutes"). Set `compliant=false` for judgment-only rule
   breaks such as "creator visible" when you can't be sure.
6. `render_clips(campaign, top_n=5)` and poll `job_status`.
7. Present the rendered clips to the user as a numbered list: title, AI score, duration, one line
   of why, file path. Then **ask** which to approve. Call `review_clips` only with what they chose.
8. `set_clip_copy` for each approved clip and each campaign platform. Include the required
   hashtags. Repeat until `issues` is empty. Show the user the copy.
9. `publish_clip(..., confirm=false)` shows exactly what would go out. Only after the user says yes,
   call it again with `confirm=true`. TikTok via Upload-Post needs a paid tier, and unaudited TikTok
   apps post privately; say so if TikTok is in the list.

## Learn from results

- `sync_metrics()` (Upload-Post) or `record_metrics(post_id, views, ...)` for numbers the user
  reads off a dashboard. `campaign_report(campaign)` for qualified views and revenue.
- `performance_insights()`, then interpret it. Save only robust findings with `save_learning`
  (one concrete sentence plus the numbers); `retire_learning` what new data contradicts. Check
  `calibration`: if judge scores don't predict views, tell the user and adjust how you score.

## Never

- approve, reject or publish without the human's explicit choice in this conversation;
- change timestamps in your head: clipper snaps them, so trust `opens_with` / `ends_with`, not your estimate;
- pre-weight scores: score each dimension honestly and let clipper apply the campaign weights.
