# Benchmark report

`ezra benchmark` run on 2026-09-23 against the final code. Machine: Apple M-series laptop
(macOS 26.6, 10 CPUs, CPU only), Python 3.11.
- Providers: faster-whisper `small` (int8, batched), local diarizer, Haar faces, heuristic scoring
  (no model).
- Full run: 325 s for four fixtures plus the deterministic suites.
- Raw results: [`benchmarks/results/20260923-000244/report.json`](benchmarks/results/20260923-000244/report.json)
  and the generated [`report.md`](benchmarks/results/20260923-000244/report.md).

Reproduce: `uv run ezra benchmark` (or `--fixtures podcast` for a 90-second check).

## What the fixtures are, and what that means for the numbers

Four ~3-minute videos synthesized at run time with exact ground truth (`src/ezra/benchmark/fixtures.py`):

| Fixture | Content |
|---|---|
| `solo` | One voice, one static talking head |
| `podcast` | Host + guest (two distinct TTS voices), wide two-shot |
| `multi` | Three voices, three faces in a row |
| `scenes` | Host + guest with 29 hard cuts between a wide shot, per-speaker close-ups and slide cutaways |

The script mixes strong moments (a $400k loss, near-quitting, contrarian fundraising takes) with
deliberately dull stretches (hiring platitudes, tool lists, a lightning round), filler words
and two long pauses.

**Read these as regression signals, not real-world accuracy.** TTS speech is cleaner than real
podcasts, the "faces" are still photographs, and "strong/dull" labels are the fixture author's
judgement.

## Results

### Speed (CPU only)

| fixture | source | analyze | × real time | candidates | render top 3 |
|---|---|---|---|---|---|
| solo | 182 s | 31.5 s | 0.17 | 0.1 s | 23.2 s |
| podcast | 179 s | 30.9 s | 0.17 | 0.1 s | 27.3 s |
| multi | 202 s | 35.0 s | 0.17 | 0.2 s | 69.9 s |
| scenes | 179 s | 36.5 s | 0.20 | 0.1 s | 60.7 s |

- Analysis (transcription, diarization, scenes, faces, silence, topics) runs at about **6× real
  time** on CPU.
- Renders take 7–29 s per 20–50 s clip. Blur layouts and many-scene clips cost more because of
  the blurred background and per-scene graphs.

### Transcription and speakers

| fixture | WER | turn onset error p50 / p90 | diarization word accuracy | speakers found / true |
|---|---|---|---|---|
| solo | 0.8% | 62 / 137 ms | 100% | 1 / 1 |
| podcast | 0.9% | 85 / 136 ms | 97.9% | 2 / 2 |
| multi | 0.7% | 80 / 143 ms | 90.4% | **2 / 3** |
| scenes | 0.9% | 85 / 136 ms | 97.9% | 2 / 2 |

- WER is after normalization (numbers spelled out, so "$400,000" = "four hundred thousand dollars").
  All four filler words in the script were transcribed on podcast, multi and scenes (3 of 4 on
  solo), so filler removal can see them.
- **Multi-speaker weakness**: the local diarizer merged the third voice (11 of 132 speech windows)
  into another speaker. For 3+ speakers or similar voices, use pyannote
  (`EZRA_DIARIZER=pyannote`, see REMAINING_EXTERNAL_SETUP.md).

### Scenes and faces

| fixture | true cuts | found | precision | recall | face recall | face precision | layout hints |
|---|---|---|---|---|---|---|---|
| solo | 0 | 0 | – | – | 100% | 100% | single |
| podcast | 0 | 0 | – | – | 100% | 100% | two_shot |
| multi | 0 | 0 | – | – | 100% | 100% | group |
| scenes | 29 | 29 | 100% | 100% | 91.9% | 94.1% | none, single, two_shot |

A face counts as detected when its centre falls inside a true portrait tile. On `scenes`, the
misses and 12 false detections come from the tightly cropped close-ups (the portrait fills and
overflows the frame).

### Candidates

| fixture | count | clean start | clean end | ends on "?" | in duration bounds | strong openings in top 5 | top 5 containing dull material | mean IoU in top 5 |
|---|---|---|---|---|---|---|---|---|
| solo | 15 | 100% | 100% | 0% | 100% | 3 / 6 | 0 | 0.09 |
| podcast | 16 | 100% | 100% | 0% | 100% | 3 / 6 | 0 | 0.08 |
| multi | 18 | 100% | 100% | 0% | 100% | 3 / 6 | 1 | 0.05 |
| scenes | 17 | 100% | 100% | 0% | 100% | 3 / 6 | 0 | 0.08 |

Top 5 on `podcast` (rank score, hook type, duration, opening):

1. 74.3 contrarian, 21 s: "I think most startups should never raise venture money. Honestly, the best companies I…"
2. 72.6 loss, 34 s: "Today my guest built a company, lost almost everything, and came back. So, um,…"
3. 72.2 number, 27 s: "We raised $2 million before we had a single paying customer, and it made…"
4. 69.4 loss, 18 s: "Was there a moment you almost quit? I almost quit the company in 2021.…"
5. 67.1 number, 50 s: "My co-founder read it, tore it in half, and said give me 90 days.…"

Reading:

- The #1 clip opens on a labelled strong moment in every fixture, and no dull section (hiring,
  tools, lightning round) opens a top-5 clip.
- Strong moments that open a top-5 clip: 3 of 6. The heuristics miss three:
  - "too proud to ask for help": the hook is mid-answer;
  - "sell before you build": the closing answer;
  - the $400k loss line itself. The #2 clip *contains* the $400k story but opens on the host's
    teaser, which works as a hook but isn't the labelled opening.
- #4 and #5 are two cuts of the same near-quitting story (IoU below the 0.45 suppression
  threshold).
- This is the gap an agent closes over MCP: reading the transcript, proposing
  `ezra_create_candidate` cuts that open on the line itself, and scoring candidates against each
  other.
- On `multi` the 51 s venture-money window still reaches #2 and runs into the dull lightning
  round. The segue trim needs a correct interviewer label, and diarization merged speakers there.

### Renders (12 clips, 3 per fixture)

Every clip passed every check:
- 1080×1920;
- H.264 + AAC at 30 fps;
- A/V duration drift < 100 ms (measured 8–15 ms);
- loudness −14 ± 1 LUFS (measured −14.0 to −14.2);
- true peak ≤ −1 dBTP (measured −4.1 to −4.4);
- ≥ 90% of captions over speech (91.7–100%);
- first caption within 400 ms of speech (−225 to +138 ms; negative means the caption leads);
- no silence > 1 s left;
- no blip of the previous word before the first words.

| fixture | layouts | silence removed per clip | crop moves / min | tracked crop on a face | split centre error |
|---|---|---|---|---|---|
| solo | track | 1.2–2.1 s | 0 | 100% | – |
| podcast | split | 1.2–2.0 s | 0 | – | 1.5% of width |
| multi | blur (group) | 0.9–3.1 s | 0 | – | – |
| scenes | mixed split/track/blur per scene | 1.2–2.0 s | 0–1.85 | 100% | 1.5–1.6% of width |

### Compliance: 17 / 17

Each case has a known answer, and each verdict came from the intended rule (inspected, not only
counted):

- Duration too short/long, profanity, a competitor in the transcript and in the copy, a rejected
  source, missing subtitles at render, a missing hashtag and a platform outside the campaign
  → FAIL.
- An unverified source, and a forbidden topic without a model verdict, with a keyword hit, or
  with a "not compliant" model verdict → REVIEW_REQUIRED.
- Clean cases and a "compliant" model verdict → PASS.

### Job queue: 6 / 6

- A transient error retried with backoff (5 s, 10 s) and completed on attempt 3.
- An always-failing job gave up after `max_attempts`.
- A `ValueError` wasn't retried.
- A cancelled job can't be claimed.
- A running job with a stale heartbeat was requeued.
- A duplicate enqueue with the same dedupe key returned the same job.

## What the benchmark found and fixed during this build

| Finding | Fix |
|---|---|
| Clips opened with an 18 ms blip of the previous word (ASR word ends run early) | Cuts move to the quietest audio point near the boundary; 10 ms fades at joins |
| The top clip ran 51 s across three topics, winning on the payoff of a *later* subject | Topic-drift penalties on retention/context, payoff not credited across subjects; windows don't end on the interviewer's segue |
| One speaker was diarized as two (word accuracy 52%) | One-speaker default unless the split is real (silhouette and merge-height jump) → 100% |
| Loudness −15.3 LUFS (loudnorm fell back to dynamic mode) | Voice compression + limiter + two-pass loudnorm → −14.1 LUFS, −4.3 dBTP |
| Captions burned "$400 ,000", "40 %", "co -founder" | ASR split tokens merged into whole words (WER 1.3–1.5% → 0.7–0.9%) |

## Not measured here

- Real-world virality or payout. That needs published posts; `ezra insights` reports rank-vs-views
  calibration once they exist.
- pyannote, WhisperX, MediaPipe and LLM/agent scoring. They are optional providers, and the
  benchmark uses the defaults.
