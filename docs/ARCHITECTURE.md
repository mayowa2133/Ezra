# Architecture

```
                  ┌────────────┐  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐
  people/agents → │ CLI (ezra) │  │ REST API     │  │ MCP server    │  │ Dashboard (Next.js)  │
                  │ typer+rich │  │ FastAPI      │  │ stdio, ezra_* │  │ → /api/ezra proxy ───┼─┐
                  └─────┬──────┘  └──────┬───────┘  └──────┬────────┘  └──────────────────────┘ │
                        └──────────────┬─┴─────────────────┘  (token held server-side)  ◄───────┘
                                       ▼
          ┌──────────────────────── services (src/ezra) ────────────────────────────┐
          │ campaigns · sources · analysis · candidates · render · review · metadata │
          │ publishing · metrics · economics · analytics · experiments · runner      │
          └───────┬───────────────────┬──────────────────────┬───────────────────────┘
                  ▼                   ▼                      ▼
          jobs (DB queue)      storage (local | S3)    secrets (env | Fernet file)
                  │                   │
                  ▼                   ▼
        worker(s) → child process per job → ffmpeg · faster-whisper · OpenCV · PySceneDetect
                  │
                  ▼
      SQLite (local) | PostgreSQL (Docker) via SQLAlchemy 2 + Alembic
```

## Principles

- **One service layer, four interfaces.** The CLI, API and MCP tools are thin: they validate
  input, call a service or enqueue a job, and format the result. Behaviour is identical across them
  and is tested through all of them (`tests/test_e2e.py`).
- **Providers behind interfaces.** Every external capability has an abstract interface, a default
  local implementation and optional alternatives, selected by configuration:

  | Capability | Interface | Implementations |
  |---|---|---|
  | Transcription | `TranscriptionProvider` | faster-whisper (default, batched), WhisperX |
  | Diarization | `Diarizer` | local MFCC + agglomerative clustering (default), pyannote community-1, none |
  | Face detection | `FaceDetector` | `auto` (default): OpenCV YuNet (227 KB MIT model fetched once), Haar with eye verification when offline; MediaPipe BlazeFace |
  | Scenes | function | PySceneDetect ContentDetector |
  | Model critique | `LLMProvider` | heuristic (default), `claude -p`, `codex exec`, any OpenAI-compatible server |
  | Storage | `Storage` | local filesystem, S3-compatible (MinIO, AWS, R2) |
  | Publishing | `Publisher` | YouTube Data API, TikTok Content Posting API, Instagram Graph API, Upload-Post, local export |
  | B-roll | `BrollProvider` | local licensed library, Pexels, generated text cards |
  | Live source | `LiveSource` | file (replayed at speed), HLS URL, YouTube live via yt-dlp |
  | Clip engine | setting | native (default), self-hosted OpenShorts |
  | Campaign source | `CampaignSourceAdapter` | file import (YAML/JSON/CSV); no scraping by design |

- **Everything long-running is a job.** Analysis, candidate generation, ranking, renders, B-roll,
  publishing, status refresh, metrics sync, campaign runs and live sessions are jobs with
  progress, logs, structured errors, retries and cancellation.

## Job system (`jobs.py`, `worker.py`, `tasks.py`)

- Jobs live in the `jobs` table: `queued → running → completed | failed | cancelled`, with
  `progress`, `message`, `result`, `error` (type, message, traceback, attempt) and `job_logs`.
- **Claiming** is atomic: `SELECT … FOR UPDATE SKIP LOCKED` on PostgreSQL, a conditional
  `UPDATE … WHERE status='queued'` on SQLite. Any number of workers can run.
- **Retries**: transient errors retry with backoff `5·2^(n−1)` s (capped at 300 s) up to
  `max_attempts`; `ValueError`, `LookupError` and `PermissionError` are permanent (bad input
  doesn't get better by retrying). Platform 429/5xx raise `RetryableError`.
- **Isolation**: each job runs in a child process (`python -m ezra.worker --run-job ID`); the parent
  heartbeats every 2 s and terminates the child when cancellation is requested. This also keeps
  OpenCV and PyAV (which bundle conflicting libavdevice builds on macOS) out of one process.
- **Recovery**: running jobs whose heartbeat is older than 90 s return to the queue.
- **Dedupe**: `enqueue(dedupe_key=…)` returns the existing queued/running job instead of a
  duplicate (two "analyze source 3" clicks make one job).
- **Maintenance**: every 15 s the worker requeues stale jobs and runs `publishing.scheduler.tick()`:
  due scheduled posts are published, processing posts are refreshed, metrics are synced every
  `EZRA_METRICS_SYNC_HOURS`.
- Where the worker runs: `ezra worker` (Docker `worker` service), embedded in `ezra serve`, and
  embedded in `ezra mcp` (so an agent session works without a separate process). The CLI runs jobs
  inline with a progress bar unless given `--queue`.

## Pipeline

### 1. Analysis (`analysis/`, `transcription/`, `diarization.py`)

`analyze_source` is resumable. Each product is stored as a `source_analyses` row with provider,
version and confidence, and skipped on re-run unless `--force`:

1. **Transcript**: faster-whisper with word timestamps (batched pipeline), cached by a version hash
   of model, settings and the segmenter version. Segments are re-cut into sentences, and a speaker
   change splits a sentence only at a pause > 0.3 s.
2. **Speakers**: MFCC embeddings per speech window, cosine average-linkage clustering, number of
   speakers chosen by silhouette. pyannote is used when configured with `HF_TOKEN`.
3. **Scenes**: PySceneDetect content detector plus per-second visual activity.
4. **Faces**: sampled at 1 fps (0.5 fps past 30 min), with per-scene layout hints: single,
   two_shot, group or none.
5. **Silence** (ffmpeg silencedetect), **topics** (TextTiling over sentences), **text signals**
   (speaking rate, Q→A transitions, laughter, emotion lexicon).

### 2. Candidates and ranking (`candidates.py`, `scoring/`)

1. **Windows**: from every sentence start, three target lengths within the campaign's duration
   bounds. Each ends on a sentence end, never on a trailing question, and never on the
   interviewer's short segue into the next subject.
2. **Features → nine factor scores** (0–100), each with a written explanation: hook, retention,
   context, emotion, novelty, discussion, payoff, visual, campaign fit. Windows spanning several
   topics lose retention and context, and their payoff isn't credited.
3. **Diversity**: greedy NMS over content score (IoU ≤ 0.45) and a cap per topic.
4. **Compliance** at the candidate stage (duration, profanity, forbidden words, competitors, source
   rights, speakers, forbidden topics, free-form rules).
5. **Ranking**:
   - If the agent scored the candidate, its scores replace the heuristic.
   - If an LLM critic ran, `0.65·LLM + 0.35·heuristic`.
   - The result is blended with the **performance prior** (the candidate's traits against this
     account's published history, shrunk toward the mean; weight grows with data up to 0.35·0.8).
   - Overlap with an already-kept candidate costs 15 points.
6. **Expected value**: Monte Carlo over a log-normal view model:
   - It uses the account's own history when there is some, and a prior otherwise.
   - The draws are run through the campaign's payout rules: threshold, CPM, per-post cap and
     remaining budget.
   - The output is EV per post, p10/p90, P(qualify) and the basis.

### 3. Rendering (`render/`)

`compose()` builds one ffmpeg filter graph per clip:

1. **EDL** (`edl.build`):
   - Long *quiet* gaps (detected silence, not merely gaps between words: music and action
     stay) shrink to 0.18 s, and filler words are cut.
   - `edl.refine_boundaries` then moves each cut to the quietest 5 ms frame within ±120 ms, never
     into a kept word.
   - Each join gets 10 ms audio fades.
   - Words are remapped onto the output timeline.
2. **Reframing plan** (`layout.plan`), per scene:
   - `track`: the crop follows the **active speaker**, the face whose mouth moves (net of head
     movement) clearly more than the others. It falls back to the most prominent face, or to
     where the motion is concentrated in a faceless action shot. It moves only after a
     confirmed jump.
   - `split`: two separated faces become stacked, zoomed halves.
   - `center`: fixed centre crop. Vertical output always fills the frame; this is the fallback for
     faceless shots without a clear subject.
   - `blur`: the whole frame over a blurred fill, only for long static shots (graphics, text
     slides), panels of equals, landscape output and scenes whose graphics can't be kept whole.
   - **Graphics protection** (`protect_graphics`, on by default):
     - Burned-in graphics are found two ways:
       - PP-OCRv3's text detector (`EZRA_TEXT_DETECTOR=auto`, a 2.4 MB opencv_zoo model fetched
         once, run on every other sample); it finds small labels inside busy panels. Offline, or
         with `EZRA_TEXT_DETECTOR=morph`, a morphological detector instead: lines of type,
         rejecting straight edges; a line that
         touches scenery is split back out of the blob when it holds most of the blob's ink;
       - per scene, detailed regions that hold still while the footage moves (roster panels,
         counters, logos).
     - Text that is part of the scene (signs, stencils, a projected screen) doesn't steer the crop
       (`faces.anchor_text`):
       - a line in the caption / lower-third band counts as burned in;
       - over moving footage, a line counts when it holds its place (≤ 1.2 px of jitter) while
         the footage around it moves; scene text drifts with the picture;
       - over still footage, a line counts when it sits in a corner, or at the same place in a
         neighbouring shot.
       Source-caption detection still sees every line.
     - A graphic counts only if it appears in half the scene's samples, and pieces on one row are
       joined. Planning also splits a shot where a large graphic comes or goes (held ≥ 0.75 s),
       so a caption that runs across a cut is judged in each part.
     - Each crop then slides (at most 60% of its half-width off its subject) so every graphic is
       fully in or fully out.
     - A large graphic (≥ 12% of the width) that no crop can keep whole sends the scene to
       `blur` (auto layout, vertical output).
     - Decisions are recorded per scene in the render record (`graphics`, `protected`).
   - **Source captions** (`yield_to_source_captions`, on by default):
     - What counts: a wide line of type (≥ 25% of the width) low in the frame, standing alone on
       its row, not inside a larger still panel, held for 2+ samples.
     - Where it applies: only while the output actually shows that caption.
     - Effect: Ezra's burned-in captions are dropped for that span, which is recorded as
       `source_captions`. The SRT/ASS sidecars keep every word.
   Faces, motion and graphics are sampled at 4 fps in one decode.
3. **Overlays**: the caption band is streamed as raw RGBA frames into ffmpeg (Pillow-drawn, seven
   themes including `pop`, matched to top Shorts; word-level highlight; optional colour emoji
   above captions with an illustratable word), hook card, logo, watermark, CTA end card,
   punch-ins, B-roll.
4. **Audio**: voice compression + limiter, then two-pass loudnorm to −14 LUFS (true peak
   ≤ −1 dBTP), 48 kHz stereo AAC.
5. **Extras**: optional intro/outro, thumbnail pick (sharpest frame with a face), SRT and ASS
   sidecars.

Each render is a new `clip_versions` row. Re-renders and platform variants keep history.

### 4. Review → publish → learn

- `review.approve` is the only path to `approved`. Metadata generation enforces required
  hashtags, mentions and CTA and platform length limits, and is compliance-checked at the
  publish stage.
- `publish_clip` is a dry run until `confirm=True`. It checks approval, account availability,
  platform rules, posting limits, the campaign window and duplicates (idempotency key per clip
  version, platform and account), then enqueues one `publish_post` job per platform.
- Adapters use official APIs (see REMAINING_EXTERNAL_SETUP.md). YouTube schedules platform-side
  with `publishAt`; other platforms are posted by the scheduler when due.
- Metrics are pulled per post (`metrics.sync`) or imported/entered manually. The post's
  `features` snapshot, taken at publish time, is what `analytics` attributes results to.

## Web dashboard (`apps/web`)

- Next.js App Router, client components, no UI framework.
- The browser only talks to `/api/ezra/*`, a server-side route that adds the API bearer token and
  streams bodies (uploads, ranged video).
- Pages: dashboard, campaigns, sources, clips, review (keyboard), publishing (preview then
  confirm), analytics, earnings, integrations, settings.

## Deployment shapes

- **Local**: `ezra serve` (API + embedded worker), SQLite and files in `EZRA_HOME`.
- **Docker Compose**: postgres, minio, one-shot `migrate`, `api`, `worker` (scale with
  `--scale worker=N`), `web`. See DEPLOYMENT.md.
