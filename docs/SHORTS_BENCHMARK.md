# Ezra vs top MrBeast Shorts

2026-09-23. Ezra's clips of "Escape 100 Cops, Win $500,000" (see REAL_FOOTAGE_TEST.md) measured side by
side with 30 of MrBeast's own Shorts, and the gaps then fixed and re-measured.

## The reference set

- **30 Shorts** from the @MrBeast channel:
  - the 20 most-viewed of his latest 150, plus his 10 most recent;
  - uploaded July 2024 – September 2026.
- **Views:** 14.8M to 1.31B each (median 773M, 19.1B total). 23 of the 30 have more than 100M.
- Downloaded at 720×1280 for internal analysis only (~143 MB), never republished.
- **Caveat:** most are *native* vertical Shorts, planned and shot for the format. Third-party clips of
  his long-form videos mostly live on TikTok and weren't collected, so this is a demanding bar for
  clips cut from long-form.

## Method

`scripts/compare_shorts.py` runs the same measurements on every clip:
- duration;
- loudness;
- scene cuts per second and median shot length;
- Whisper word timings: time to first word, words per minute, share of time with speech, longest
  pause, the words in the first two seconds, and the last words;
- letterboxed share: a 16:9 picture inset over flat padding, requiring the inset's full-width edges,
  so dark or plain filled frames don't count;
- faces (share of frames with one, size, horizontal position).

Contact sheets of opening frames and of 8 frames across each clip covered what numbers can't
(caption style, graphics, hook cards).

The letterbox detector was validated against Ezra's render records. It had no false positives, and
it under-counts slightly on dark inset pictures. For Ezra's clips the table uses the render records,
which are exact.

## Results

| Metric (median) | 30 top MrBeast Shorts | Ezra before (iteration 7) | Ezra after (iteration 8) |
|---|---|---|---|
| Length | 35.7 s | 23.0 s | 28.1 s |
| First spoken word | 0.0 s | 0.0 s | 0.0 s |
| Cuts per second | 0.51 | 0.49 | 0.52 |
| Median shot | 1.4 s | 1.6 s | 1.6 s |
| Words per minute | 174 | 199 | 204 |
| **Share of time with speech** | **0.66** | 0.82 | **0.69** |
| Longest pause | 1.7 s | 1.2 s | 1.9 s |
| **Letterboxed screen time** | **0%** | 28% (mean) | **2%** (mean; 7 of 8 clips at 0%) |
| Frames with a face | 78% | 47% | 45% |
| Loudness | −14.2 LUFS | −14.0 | −14.1 |

**What the pictures showed (benchmark):**
- Every frame is filled and vertical, bright, with a person in shot from frame one.
- Captions are sparse: one or two words, sentence case, white with a thin outline, near the
  action, emphasis words colored. They're often absent while the picture carries the moment.
- Big text is reserved for stakes: money amounts, countdowns, counters.
- There's no title-card box.
- The first sentence states the premise ("How many people does it take to stop Ronaldo?", "For
  $10,000, will you go to the North Pole?") or is a reaction ("Bear! Bear! Bear!").
- The ending resolves on a reveal or reaction ("It floats!", "You won!").

**What ours showed before:**
- A title card on every clip.
- Big 2–3-word uppercase captions on every word.
- Openings on connectors ("The reason I've been…", "But what the cops…", "You know, we…",
  "And we got intel…").
- Some endings mid-action ("Hold on, hold on. It opens.").
- Letterboxing on many faceless shots.
- Talkier clips (82% speech): gaps holding music and action were scored as "dead air".

## What changed (iteration 8)

1. **Vertical output always fills the frame.** Faceless shots follow the action or fill from the
   centre; only a long *static* shot (a graphic or text slide) is shown whole.
2. **Action isn't dead air.** Retention counts only truly quiet time, so windows with reactions, crashes
   and cheering are no longer penalized. Speech share went from 0.82 to 0.69 (benchmark 0.66).
3. **Openings that lean on earlier context** ("you know", "oh yeah", "the reason", "and…") are
   penalized.
4. **The critic's brief carries the benchmark findings:**
   - premise-first or reaction-first openings, with the benchmark's own openings as examples;
   - resolution endings;
   - room for the action;
   - typical lengths of 20–50 s.
5. **A benchmark-matched caption theme, `pop`:** 1–2 words, sentence case, white with a thin outline,
   mid-frame, emphasis words (money, numbers, stakes) in yellow. It's applied to the test campaign
   through a brand kit, and the global default stays `bold`.

The resulting openings:
- "You're under arrest! Jimmy, they're on us…"
- "We are so close. Oh my gosh, there are so many cops."
- "Let me introduce you to my doppelganger."
- "I'm currently sneaking through the sewer system…"

## Verdict

| Dimension | Stacks up? |
|---|---|
| Pacing (cuts, shot length), loudness, instant start | **Yes**, matches within a few percent |
| Framing (fills the vertical frame) | **Yes** after iteration 8 (2% vs 0%) |
| Speech/action balance | **Yes** after iteration 8 (0.69 vs 0.66) |
| Caption style | **Close** with the `pop` theme |
| Openings | **Mostly.** Most now open on a reaction or the premise; a few still open on narration from the long-form ("Even in a city entirely designed to…") |
| Length | **Shorter** (28 s vs 36 s); within the benchmark's spread (19–48 s) |
| Faces on screen | **No** (45% vs 78%). Native Shorts are shot around faces; this long-form source has long faceless action and night-vision stretches |
| Title card | **Open question.** The benchmark uses none, but it's native content with a spoken premise; clips from long-form usually need a stated premise. Decide with an A/B experiment once posting (`ezra experiment create … hook_overlay`) |

Where a clip from long-form can't match native Shorts (faces-first composition, a premise scripted for
the format), the gap comes from the source, not the editing.

## Remaining work

- **Face-aware selection:** favour windows where people are on screen (the benchmark's 78%).
- **Protect the source's own graphics** (counters, name bars) from cropping. This needs text/UI
  detection.
- **A "premise prefix"** for clips whose moment needs context: a 2–3 s line from the video's cold open
  stitched in front, the spoken equivalent of the title card.
- **Collect third-party clips** of long-form videos (the like-for-like reference) through an allowed
  source.

Reproduce: download a reference set you're allowed to analyse, then
`uv run python scripts/compare_shorts.py out.json bench:ref/*.mp4 ezra:clips/*.mp4` (one LABEL:PATH
argument per file).
