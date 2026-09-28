# Remediation plan

Written 2026-09-27 after reading the docs, the git history (42 commits) and the publishing,
metrics and analytics code. The code was treated as the source of truth. Two findings changed the
order: YouTube scheduled posts are never uploaded, and a retried upload can create a duplicate
video.

No Google OAuth client is available in this environment, so YouTube work is verified against mocks
of the documented API. A one-command live test is provided for when credentials exist, and nothing
is labelled live-verified that wasn't.

Reference repo `vfarcic/youtube-automation` has **no license** (GitHub reports none; no LICENSE
file), so it is all-rights-reserved. It is read for concepts only; no code is copied.

## Findings, by priority

Severity: **critical** (wrong behaviour or data loss), **high** (blocks real use), **medium**,
**low**. Value is the expected effect on clip quality, posting reliability and earnings.

| # | Limitation observed | Severity | Value | Approach | Tests |
|---|---|---|---|---|---|
| 1 | **Scheduled YouTube posts never upload**: `publish_clip` queues only immediate posts, and the scheduler skips YouTube on the assumption that YouTube schedules them. `run_publish` also never passes `scheduled_at`, so `publishAt` is never sent | critical | high | Queue YouTube uploads immediately with `publishAt`; pass the schedule through; store the requested and the returned state | Service-level schedule test with the mocked API; the scheduler never re-queues a YouTube post |
| 2 | **A retry can duplicate a video**: a new upload session per attempt, so a lost response after a finished upload re-uploads | critical | high | Persist the resumable session URI on the post; on retry, query the session (`Content-Range: bytes */N`) and resume or recover the finished video id | Interrupted-upload and lost-response tests assert one video |
| 3 | Whole file read into memory; no chunking or progress | high | medium | Chunked resumable upload (configurable chunk size, multiple of 256 KiB), progress callback, per-chunk retry with backoff on 429/5xx/connection errors | Chunk-range tests, resume-after-308 tests, memory bounded by chunk size |
| 4 | Errors are strings: quota, revoked token and invalid input look alike; retried or not by status code only | high | high | `PublishError(code, retryable)` with YouTube reason parsing (`quotaExceeded` permanent for the day, `rateLimitExceeded` retryable, `invalid_grant` → account needs reconnect, 400 input errors permanent); `error_code` persisted on posts | One test per failure mode listed in the mission |
| 5 | No thumbnail upload | high | medium | `thumbnails.set` after upload; JPEG/PNG validation, 2 MB cap, dimension sanity; failure is a warning on the post, never a failed upload | Unit + mocked integration tests |
| 6 | Unaudited Google projects lock uploads to private; Ezra would silently post "public" that stays private | high | high | Compliance mode: `EZRA_YOUTUBE_PUBLIC_ALLOWED` (default off) refuses public/scheduled uploads with an explanation; returned privacy compared with requested | Tests for refusal, and for a mismatch warning |
| 7 | CLI connect needs the API running; no disconnect, no token health, scopes miss analytics | high | medium | Loopback listener for `ezra accounts connect youtube`; scopes add `yt-analytics.readonly`; opt-in `youtube.force-ssl` for edit/delete; disconnect revokes and deletes the token; token health check | OAuth flow tests with mocks (state, PKCE, refresh, revoked) |
| 8 | Metadata not sanitized for YouTube's limits (tag total 500 chars, `<`/`>` rejected, 100-char titles, 5000-byte descriptions) | medium | medium | Sanitizer before upload; problems shown in the dry run | Unit tests |
| 9 | No YouTube Analytics (watch time, average view %, subscribers) | high | high | New module `publishing/youtube_analytics.py`, windowed report per video, stored as snapshots with raw payload, window and source; new snapshot columns | Normalization tests on real response shapes |
| 10 | Learning ignores retention; insights lack N/confidence wording | medium | high | Outcomes add average view %, watch time, subscribers; interpretable statements with N and a confidence label; no causal claims | Analytics tests with synthetic post history |
| 11 | No update/delete of posted videos | medium | medium | `update_video` / `delete_video` (manage scope), confirm-gated in service, CLI, API, MCP | Mocked tests; refusal without confirm |
| 12 | Lifecycle is implicit (publishing/scheduled/published/failed/cancelled) | medium | medium | Keep `status` compatible; add `platform_state` (uploading, processing, private, scheduled, public, deleted) and `upload_progress` | Lifecycle transition tests |
| 13 | Moment selection: same-story duplicates, openings on setup lines, weak heuristic confidence | high | high | Story-level dedupe on content overlap (not only timestamps); start/end variants scored per sentence; `ezra run --agent claude` wraps the critic with batching and fallback | Benchmark: strong-moment recall and duplicate rate, before vs after |
| 14 | No repeatable real-footage benchmark | medium | medium | `scripts/real_footage_bench.py` computing measurable values on already-ingested real sources | Run on the three MrBeast sources |
| 15 | Dashboard shows integrations but not YouTube health or compliance mode | medium | medium | Integrations page: channel, scopes, token health, mode, reconnect/disconnect, test private upload | Type check, lint, API tests |
| 16 | MCP lacks account listing detail, post inspection, analytics tools | medium | medium | Tools: list accounts, inspect post, sync/analyze YouTube, update/delete (confirm) | MCP stdio test |
| 17 | No compliance doc | medium | high for API audit | `docs/YOUTUBE-COMPLIANCE.md` | Review |
| 18 | Diarization weak on 3+ speakers; active speaker with similar faces | medium | low for current content | Measure only unless time permits; pyannote path already exists | Existing benchmark |

## Order of work

1. Findings 1–8, 12 (the YouTube publisher and publishing service), one migration.
2. Finding 9–10 (analytics, learning).
3. Findings 11, 15–17 (update/delete, dashboard, MCP, compliance doc), live-test script.
4. Findings 13–14 (moment selection, real-footage benchmark), measured before and after.
5. Reports: BUILD_REPORT, NEXT_STEPS, REMAINING_EXTERNAL_SETUP, ARCHITECTURE, TESTING,
   OSS-LICENSES, YOUTUBE_INTEGRATION_REPORT.

Every step: tests first-class, full suite before commit, small commits.
