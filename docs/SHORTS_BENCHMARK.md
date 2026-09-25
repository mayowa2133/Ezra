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

## Round 2: faces, speakers, lulls (iterations 9–10)

The face gap sent this round to the detector first. On 24 random frames of the source, Haar found
faces in 8 (one a false positive on a name-bar icon) where a person can see about 15. YuNet
(OpenCV's CNN face detector) found 15. It became the default, and both sets were **re-measured with
YuNet**: the benchmark's face share is 93% under YuNet, not the 78% Haar suggested.

Changes:
- **Face-aware selection:** a face in the opening seconds adds to the hook score; long stretches with
  nobody on screen cost retention; the critic sees face coverage per clip and per sentence.
- **Active speaker:** the tracker frames the face whose mouth moves (net of head movement) clearly
  more than the others.
- **Groups on vertical output:** frame the densest cluster of people instead of letterboxing (a
  landscape panel stays whole).
- **Lulls count as quiet:** the music bed dropping below the source's median loudness for 2+ s is
  cut like silence, while loud action between lines stays.
- **Minimum length:** when edits would push a clip under the campaign minimum, the render backs off
  (silence only, then no cuts) instead of producing a failing clip.

| Metric (median; faces measured with YuNet) | 30 top MrBeast Shorts | Ezra, iteration 10 |
|---|---|---|
| Letterboxed | 0% | **0%** (all 8 clips) |
| Cuts per second | 0.51 | 0.48 |
| Longest pause | 1.7 s | **1.6 s** (was 4.5 s before lulls) |
| Share of time with speech | 0.66 | 0.73 |
| Frames with a face | 93% | **64%** (source average: 55%) |
| Face width (share of frame) | 0.10 | 0.18 (tighter crops of a horizontal source) |
| Length | 36 s | 24 s |
| Loudness | −14.2 LUFS | −14.1 LUFS |

## Round 3: the source's own graphics (iteration 11)

MrBeast's long-form videos carry burned-in graphics: a prisoner roster (JIMMY / NOLAN / TAREQ /
DARIUS / ALISON), a caught counter, name bars, body-cam timestamps and the editors' own captions.
A 9:16 crop of a 16:9 frame keeps only 32% of the width, so it sliced through them: one clip read
"…OK ON HIM RIGHT" instead of "HEY GUYS! HE'S GOT 100K ON HIM RIGHT HERE".

**Detection:**
- **Text detector.** It finds lines of type, rejecting straight edges; a pipe's edge had passed
  the first version.
- **Stillness detector.** Per scene, it finds detailed regions that hold still while the footage
  moves. That is what catches the roster: its name labels merge with the panel border into one
  blob too tall to pass as text.
- **Guards:**
  - a graphic must appear in half a scene's samples;
  - stillness needs at least 5 samples (1.2 s);
  - a scene with more than 4 still regions is scenery (a slow push-in), not graphics.

**Decision:**
- Crops slide so each graphic is fully in or fully out, staying within 60% of the crop's
  half-width of the subject.
- If a large graphic can't be kept whole, the scene is shown whole.

**Results on the 8 test clips** (every flagged frame checked by eye):

| Clip | Decision | What it was |
|---|---|---|
| 6 | shown whole (2.3 s, across two shots); Ezra's captions off for 16.4–18.5 s | The editors' caption "HEY GUYS! HE'S GOT 100K ON HIM RIGHT HERE", now readable and not doubled |
| 8 | shown whole (2.0 s) | The prisoner roster as it slides in |
| 12 | shown whole (6.2 s, 3 scenes) | The prisoner roster, on screen while the cops search |
| 10, 12 | crop moved | Text kept whole with the speaker still framed |
| 7, 11, 13 | unchanged | Graphics (channel logo, corner counters) already outside the crop |
| 9 | unchanged (miss) | The roster also appears here while its highlights animate; neither detector catches it, so it is still cropped |

- **False positives:** the ones found were fixed before these numbers were taken:
  - a pipe edge;
  - a treeline in a slow push-in;
  - rooftops in an aerial shot;
  - grass in a body-cam shot;
  - the roster's name row read as a caption.
- **Cost:** 4.6% of total screen time is shown whole, up from 0%, and all of it is deliberate.
  The benchmark Shorts don't have this problem, because they are composed for the vertical frame
  from the start.
- **Speed:** detection adds about 1 s of render time per 30 s clip.
- **Double captions:** while the source's own caption is on screen, Ezra's burned-in captions
  step aside. The SRT sidecar keeps every word.
- **Caption detection:** real captions separated cleanly from look-alikes on these clips. A
  caption grazing a door frame keeps 85–98% of its blob's ink in its line; texture and panel rows
  keep 30% or less. A caption must also stand alone on its row, which rules out a roster's name
  labels.

## Verdict

| Dimension | Stacks up? |
|---|---|
| Pacing (cuts, shot length), loudness, instant start | **Yes**, matches within a few percent |
| Framing (fills the vertical frame) | **Yes**: 0% letterboxed after iteration 10; 4.6% shown whole after iteration 11, only where the source's own graphics (captions, roster) would otherwise be sliced. Groups are framed on the densest cluster and the active speaker is tracked |
| Speech/action balance | **Yes**: 0.73 speech share vs 0.66, longest pause 1.6 s vs 1.7 s |
| Caption style | **Close** with the `pop` theme |
| Openings | **Mostly.** Most now open on a reaction or the premise; a few still open on narration from the long-form ("Even in a city entirely designed to…") |
| Length | **Shorter** (24 s vs 36 s); inside the benchmark's spread (19–48 s) |
| Faces on screen | **Partly** (64% vs 93%). The clips beat the source's own 55% (selection favours faces), but native Shorts are shot around faces and this source has long faceless action and night-vision stretches |
| Title card | **Open question.** The benchmark uses none, but it's native content with a spoken premise; clips from long-form usually need a stated premise. Decide with an A/B experiment once posting (`ezra experiment create … hook_overlay`) |

Where a clip from long-form can't match native Shorts (faces-first composition, a premise scripted for
the format), the gap comes from the source, not the editing.

## Remaining work

- **Animated graphics:** a panel whose highlights change (the roster in clip 9) escapes both
  detectors. A small OCR/text-detection model (e.g. OpenCV's DB text detector) would catch it, at
  the cost of another model download.
- **A "premise prefix"** for clips whose moment needs context: a 2–3 s line from the video's cold open
  stitched in front, the spoken equivalent of the title card.
- **Collect third-party clips** of long-form videos (the like-for-like reference) through an allowed
  source.

Reproduce: download a reference set you're allowed to analyse, then
`uv run python scripts/compare_shorts.py out.json bench:ref/*.mp4 ezra:clips/*.mp4` (one LABEL:PATH
argument per file).
