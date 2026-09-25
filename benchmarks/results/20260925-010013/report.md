# Ezra benchmark: 2026-09-25T01:00:13-04:00

Machine: macOS-26.6.2-arm64-arm-64bit, Python 3.11.6, 10 CPUs. Total 82.6s.

Providers: whisper `small`, diarizer `local`, faces `auto`, scoring heuristic (no model).

## Speed

| fixture | duration s | analyze s | × realtime | candidates s | render top 3 s |
|---|---|---|---|---|---|
| podcast | 179.001 | 39.1 | 0.218 | 0.2 | 40.2 |

## Transcription and speakers

| fixture | WER | sub/del/ins | turn onset err p50/p90 s | diarization word acc | speakers found/true |
|---|---|---|---|---|---|
| podcast | 0.009 | 5/0/0 | 0.085/0.136 | 0.979 | 2/2 |

## Scenes and faces

| fixture | true cuts | found | precision | recall | face recall | face precision | false faces | layout hints |
|---|---|---|---|---|---|---|---|---|
| podcast | 0 | 0 | 1 | 1 | 1 | 1 | 0 | two_shot |

## Candidates

| fixture | count | clean start | clean end | ends on ? | in duration bounds | strong openings@5 | #1 opens strong | top 5 with dull material | mean IoU top 5 |
|---|---|---|---|---|---|---|---|---|---|
| podcast | 16 | 1 | 1 | 0 | 1 | 0.5 | yes | 0 | 0.024 |

Top 5 on `podcast`:

1. 76.93 (contrarian, 21.0s): I think most startups should never raise venture money. Honestly, the best companies I…
2. 75.65 (loss, 34.4s): Today my guest built a company, lost almost everything, and came back. So, um,…
3. 73.51 (number, 26.5s): We raised $2 million before we had a single paying customer, and it made…
4. 69.49 (loss, 17.5s): Was there a moment you almost quit? I almost quit the company in 2021.…
5. 67.06 (question, 15.6s): If you started over tomorrow with $0, what would you do first? I would…

## Renders

| fixture | clip | layout | dur s | render s | A/V diff s | LUFS | dBTP | captions over speech | 1st caption − speech s | silence removed s | crop moves/min | track on face | split err | all checks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| podcast | 1 | split | 21 | 8.8 | 0 | -14.1 | -4.3 | 1 | -0.078 | 0 | 0 | – | 0.015 | yes |
| podcast | 2 | split | 33.61 | 17.4 | 0.008 | -14.1 | -4.4 | 1 | -0.021 | 0.54 | 0 | – | 0.015 | yes |
| podcast | 3 | split | 25.9 | 13.9 | 0.007 | -14.1 | -4.4 | 1 | 0.138 | 0.25 | 0 | – | 0.015 | yes |

## Compliance cases: 17/17

| case | stage | expected | got | ok |
|---|---|---|---|---|
| clean candidate | candidate | PASS | PASS | yes |
| too short (8s) | candidate | FAIL | FAIL | yes |
| too long (75s) | candidate | FAIL | FAIL | yes |
| profanity | candidate | FAIL | FAIL | yes |
| competitor mention | candidate | FAIL | FAIL | yes |
| unverified source rights | candidate | REVIEW_REQUIRED | REVIEW_REQUIRED | yes |
| rejected source rights | candidate | FAIL | FAIL | yes |
| forbidden topic, no model verdict | candidate | REVIEW_REQUIRED | REVIEW_REQUIRED | yes |
| forbidden topic, keyword hit | candidate | REVIEW_REQUIRED | REVIEW_REQUIRED | yes |
| forbidden topic, model says compliant | candidate | PASS | PASS | yes |
| forbidden topic, model says violation | candidate | REVIEW_REQUIRED | REVIEW_REQUIRED | yes |
| render without subtitles | render | FAIL | FAIL | yes |
| render with subtitles | render | PASS | PASS | yes |
| publish copy missing hashtag | publish | FAIL | FAIL | yes |
| publish copy ok | publish | PASS | PASS | yes |
| publish to a platform outside the campaign | publish | FAIL | FAIL | yes |
| publish copy mentions competitor | publish | FAIL | FAIL | yes |

## Job queue: 6/6

| behaviour | ok | detail |
|---|---|---|
| transient_error_retried | yes | status=completed, attempts=3, backoff_seconds=[5, 10] |
| gives_up_after_max_attempts | yes | status=failed, attempts=3 |
| permanent_error_not_retried | yes | status=failed, attempts=1 |
| cancelled_job_not_claimed | yes | status=cancelled |
| stale_running_job_requeued | yes | requeued=[5] |
| duplicate_enqueue_deduplicated | yes |  |
