# Decisions

Short records of the choices that shape Ezra: context, decision, consequences.

## D1. One service layer, four thin interfaces

The CLI, REST API, MCP server and dashboard all call the same Python services. Anything a person
can do, an agent can do through the same code path, with the same checks. Interfaces stay thin
(validate, call, format), and `tests/test_e2e.py` drives all four.

## D2. No paid model API by default; agents judge through MCP

The user runs Claude Code / Codex on subscriptions and asked for MCP, not API keys. So Ezra's
default scoring is deterministic heuristics with written explanations, and the high-judgement work
happens in the agent driving the MCP tools (`ezra_create_candidate`, `ezra_score_candidate`).
Optional critics shell out to `claude -p` / `codex exec` (the user's login) or an
OpenAI-compatible **local** server.

Consequence: every model output is advisory. Weights, the prior, diversity and compliance are
applied by Ezra.

## D3. Agents and models score factors; Ezra applies the weights

A model asked for "a score" drifts between runs and can't be calibrated. Agents give nine 0–100
factor scores against each other, and `heuristic.content_score` applies the campaign's weights.
Scores stay comparable across runs, so rank-vs-views calibration and the weight optimizer mean
something.

## D4. Compliance is separate from quality, and staged

A great clip that breaks a rule is worthless; a rule nobody can check automatically still needs a
human. Rules are derived at import, carry a severity (fail / review / info), and are evaluated
independently of scoring at three stages (candidate, render, publish). Unverifiable rules default
to REVIEW_REQUIRED instead of pretending to pass.

## D5. Rank by expected earnings, show the uncertainty

CPM programmes pay nothing below a threshold and cap per clip, so views don't map linearly to
money. EV comes from a Monte Carlo over the account's own view distribution (shrunk toward a prior
when there is little data), run through the campaign's payout rules. The card shows p10–p90,
P(qualify) and the basis ("prior" vs history), so a guess isn't mistaken for a forecast.

## D6. Learn with shrinkage, speak only when the evidence clears the bar

Early results are noisy, and small samples produce confident nonsense. So:
- cohorts are shrunk toward the mean (pseudo-count 5);
- observations are emitted only when the 80% interval excludes "no effect";
- the prior's weight grows with sample size and is capped;
- experiments need a minimum sample per variant and P(best) > 0.9 before declaring a winner.

## D7. A database-backed job queue instead of Redis/Celery

Ezra must run as one process on a laptop and scale out on a server. The jobs table works on
SQLite and PostgreSQL, where `FOR UPDATE SKIP LOCKED` makes claiming safe for many workers. Jobs
already need persistence (progress, logs, errors, retries, cancellation, dedupe) for the dashboard
and agents. Throughput needs are small (a few jobs per minute).

## D8. Each job in a child process

- ffmpeg/whisper steps must be cancellable immediately.
- A crashing native library must not kill the worker.
- OpenCV and PyAV bundle conflicting libavdevice builds on macOS.

A child process per job, heartbeated by the parent, handles all three. The per-job interpreter
start (~1 s) is negligible next to media work.

## D9. Render with ffmpeg + Pillow, not Remotion or libass

Remotion needs a paid company license for many businesses and a headless browser. libass
availability varies by ffmpeg build. Captions and cards are drawn with Pillow and streamed into
ffmpeg as raw RGBA frames through one filter graph. Result: identical output on any ffmpeg build,
word-level animation, full control of themes, and SRT/ASS sidecars for platforms that want text
tracks.

## D10. Reframing is decided per scene, and split screen needs evidence

One crop rule doesn't fit a podcast. Each scene gets `track` (one face), `split` (two separated
faces, zoomed halves stacked), `blur` (slides/B-roll/groups) or `center`. The tracked crop moves
only after a confirmed jump (hysteresis), which avoids jitter. Captions move to the seam in split
mode so they never cover a face.

## D11. Cuts snap to words, then to the waveform

ASR word timestamps are ±50–100 ms, so cutting on them leaves blips of the previous word or clips
the next. Candidate bounds snap to word edges with padding (`snap`). At render time each cut moves
to the quietest 5 ms frame within 120 ms (never into a kept word), and joins get 10 ms fades. The
benchmark measures leading blips.

## D12. Local diarization by default, with a one-speaker default

pyannote is better but gated (HF account + terms) and pulls PyTorch. The default is a numpy MFCC +
agglomerative clustering diarizer with no downloads. It only declares multiple speakers when the
split is real (silhouette ≥ 0.25, or ≥ 0.15 with a clear jump in the final merge), because a false
split corrupts transcripts, speaker rules and interview logic. Similar voices and 3+ speakers are
where it's weak; the docs point to pyannote there.

## D13. Official publishing APIs only, local export by default

Password automation breaks platform terms and gets accounts banned. Adapters use the YouTube Data
API, TikTok Content Posting API and Instagram Graph API with OAuth + PKCE and resumable uploads,
plus an optional aggregator (Upload-Post). Local export works with no accounts. Unaudited TikTok
apps can only post SELF_ONLY, and Ezra says so rather than failing silently.

## D14. Secrets never in ordinary tables

Tables are dumped, backed up and shown in dashboards. Rows hold references, and values live in the
environment or a Fernet-encrypted file keyed by `EZRA_SECRET_KEY`. Consequence: losing the key
means reconnecting accounts, which is documented.

## D15. Campaigns come from files, not scraping

Marketplaces' terms generally forbid scraping, and their pages change. `CampaignSourceAdapter`
has one implementation (file import: YAML/JSON/CSV) and is the seam for official APIs if
marketplaces offer them.

## D16. Synthetic, generated test media

Committed media bloats the repo and raises licensing questions. Fixtures are generated at test time
(system TTS + a public-domain portrait) with exact ground truth, which also makes the benchmark
possible. The cost: synthetic speech is easier than real podcasts. Benchmark numbers are
regression signals, not real-world accuracy claims.

## D17. OpenShorts as a peer detector, not the pipeline

OpenShorts (MIT core) finds moments with its own model. Ezra imports them as ordinary candidates
that are snapped, scored, compliance-checked and rendered by Ezra, so both detectors compete on
the same criteria. Nothing from its commercially licensed `cloud/` directory is used.

## D18. MinIO for local S3, swappable

The compose file needs an S3-compatible store that runs locally. MinIO's server is AGPL-3.0 and is
run unmodified as a separate service over the S3 API, so Ezra's code isn't affected. Docker Hub no
longer serves `minio/minio`, so the image comes from `quay.io/minio/minio`. Any S3-compatible
store can replace it via `EZRA_S3_*`.

## D19. OpenCV pinned to headless 4.x

OpenCV 5 dropped the bundled Haar cascades, and PySceneDetect/MediaPipe depend on the GUI OpenCV
wheels, which install over the same `cv2` module. A uv override excludes the GUI wheels, and the
Docker build asserts the importable `cv2` is 4.x with cascades. This was found when the container
failed at face detection.

## D20. Speech loudness: compress, then two-pass normalize to −14 LUFS

Single-pass loudnorm undershot by 1–2 LU. Speech peaks sit far above its average loudness, so
reaching −14 LUFS linearly would break the −1.5 dBTP ceiling, and loudnorm silently falls back to
dynamic mode. Gentle voice compression (4:1 above −24 dB) and a safety limiter come before a
measured two-pass loudnorm. The benchmark checks both loudness (±1 LU) and true peak (≤ −1 dBTP).
