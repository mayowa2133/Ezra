# Real-footage test: challenge-style content (MrBeast)

2026-09-23. First run of Ezra on real long-form footage: "Escape 100 Cops, Win $500,000" (20:29,
1080p), recorded as `rights_basis=permission` on the user's statement of authorization, for internal
testing only. Nothing was published. Campaign: `mrbeast-test` (15–60 s; brief: challenge moments).

Why this is a hard case for a tool tuned on podcasts:
- 543 hard cuts (one every ~2.3 s);
- music under almost all speech (1 s of true silence in 20 minutes);
- a brick-walled mix (5 LU between quiet and loud);
- many speakers;
- faces only detectable in 38% of frames;
- two sponsor segments (a book, and Call of Duty Mobile woven into the story).

## Iterations

| # | Change | What the output showed |
|---|---|---|
| 1 | Baseline | 2 of 8 clips cut to 13 s and 8.5 s (the "silence removal" deleted the chase, since word gaps ≠ silence under music); 2 were the book ad; 1 opened mid-sentence ("Beast video…": "Mr." read as a sentence end); a near-duplicate; mostly letterboxed "blur" framing |
| 2 | Cut only gaps that are actually quiet; re-check duration on the rendered clip; abbreviations don't end sentences; story-level dedupe; ad-read detection | Durations 22–34 s with the action intact; mid-sentence opening gone; one ad left (its ad phrases straddled the window edge) |
| 3 | Sponsor **segments** detected across the whole transcript (plus the sponsor's name learned, so woven-in mentions count); challenge/danger vocabulary; campaign-brief-aware hook; per-source loudness energy | Both sponsor segments out of the top 8; the video's cold-open stakes line reached #4; still weak openings ("Yeah, it's just underneath all the money…") |
| 4 | Model critic (`EZRA_LLM=claude-cli`), with an editorial brief on what high-performing clips share; batched and parallel (a single 25-candidate call timed out at 900 s) | Selection and hooks clearly better: "He trapped 100 cops inside a theater", "He broke into police HQ to save his team"; one duplicate story slipped through |
| 5 | Duplicates judged on final scores; render_top never renders two cuts of one story; faceless shots follow concentrated motion; groups follow a dominant face; short faceless shots fill from the centre; 4 fps layout sampling | No duplicates; letterboxed share of screen time **55% → 22%** |

Bugs found and fixed along the way, beyond quality:
- **`ezra run` hung forever** when a job failed transiently. The CLI didn't run its own retries.
- **Concurrent first database access** raced Alembic migrations.
- **A stale transcript was reused** after a segmentation upgrade.
- **A model timeout** killed the whole run instead of falling back to heuristics.

## Final output (iteration 5, top 8 of 40 candidates)

| clip | critic hook | length | letterboxed |
|---|---|---|---|
| 6 | Winning mom $100K through city sewers | 34 s | 9% |
| 7 | He trapped 100 cops inside a theater | 21 s | 46% |
| 8 | Escaping 100 real cops with secret rooms | 35 s | 22% |
| 9 | He snuck INTO the police headquarters | 27 s | 38% |
| 10 | Cops took my detonator — big mistake | 16 s | 0% |
| 11 | Final minutes: 100 cops, one dead end | 56 s | 28% |
| 12 | Genius plan or the dumbest thing ever? | 22 s | 0% |
| 13 | Using friend's arrest as a diversion to escape | 23 s | 26% |

Runtime for the 20.5-minute source: 8.5 minutes end to end. That covers transcription and analysis,
critic scoring of 25 candidates in 4 parallel batches, and 8 renders.

Regression check after the changes: full test suite (79 passed) and the four-fixture benchmark
(every render check still passes; top-5 overlap 0.08 → 0.02).

## What this can and can't tell you

- It shows the clips have the **structure** of high-performing ones: a standalone hook in the first
  seconds, one story, 16–56 s, a filled vertical frame, synced captions, no ads, no duplicates.
- It **can't** tell you views. That needs publishing and `ezra insights` over ~20 posts.
- Model-critic rankings vary a little between runs (iterations 4 and 5 picked overlapping but not
  identical top 8s).

## Remaining gaps on this style of content

1. **The source's own on-screen text gets cropped** when a shot is filled ("…S GOT 100K ON HI…").
   Fix: detect burned-in text (OCR) and widen the crop or letterbox those shots.
2. **Diarization finds one speaker** under the music (the local diarizer can't separate voices over
   a music bed). pyannote is the fix; it matters little here because the critic, not the speaker
   labels, picks moments.
3. **Without the model critic** (heuristics only), openings are weaker. For challenge content, run
   with `EZRA_LLM=claude-cli` (or an agent over MCP).
4. **Critic boundaries**: the critic chooses among Ezra's cuts but can't move them. Letting it
   propose tighter starts ("open on the strongest line") is the next quality step.
