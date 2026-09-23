# Ezra benchmark: 2026-09-23T00:02:44-04:00

Machine: macOS-26.6.2-arm64-arm-64bit, Python 3.11.6, 10 CPUs. Total 325.4s.

Providers: whisper `small`, diarizer `local`, faces `haar`, scoring heuristic (no model).

## Speed

| fixture | duration s | analyze s | × realtime | candidates s | render top 3 s |
|---|---|---|---|---|---|
| solo | 182.154 | 31.5 | 0.173 | 0.1 | 23.2 |
| podcast | 179.001 | 30.9 | 0.172 | 0.1 | 27.3 |
| multi | 201.788 | 35 | 0.173 | 0.2 | 69.9 |
| scenes | 179.001 | 36.5 | 0.204 | 0.1 | 60.7 |

## Transcription and speakers

| fixture | WER | sub/del/ins | turn onset err p50/p90 s | diarization word acc | speakers found/true |
|---|---|---|---|---|---|
| solo | 0.008 | 4/0/0 | 0.062/0.137 | 1 | 1/1 |
| podcast | 0.009 | 5/0/0 | 0.085/0.136 | 0.979 | 2/2 |
| multi | 0.007 | 4/0/0 | 0.08/0.143 | 0.904 | 2/3 |
| scenes | 0.009 | 5/0/0 | 0.085/0.136 | 0.979 | 2/2 |

## Scenes and faces

| fixture | true cuts | found | precision | recall | face recall | face precision | false faces | layout hints |
|---|---|---|---|---|---|---|---|---|
| solo | 0 | 0 | 1 | 1 | 1 | 1 | 0 | single |
| podcast | 0 | 0 | 1 | 1 | 1 | 1 | 0 | two_shot |
| multi | 0 | 0 | 1 | 1 | 1 | 1 | 0 | group |
| scenes | 29 | 29 | 1 | 1 | 0.919 | 0.941 | 12 | none, single, two_shot |

## Candidates

| fixture | count | clean start | clean end | ends on ? | in duration bounds | strong openings@5 | #1 opens strong | top 5 with dull material | mean IoU top 5 |
|---|---|---|---|---|---|---|---|---|---|
| solo | 15 | 1 | 1 | 0 | 1 | 0.5 | yes | 0 | 0.094 |
| podcast | 16 | 1 | 1 | 0 | 1 | 0.5 | yes | 0 | 0.083 |
| multi | 18 | 1 | 1 | 0 | 1 | 0.5 | yes | 1 | 0.053 |
| scenes | 16 | 1 | 1 | 0 | 1 | 0.5 | yes | 0 | 0.083 |

Top 5 on `solo`:

1. 75.69 (number, 26.6s): We raised $2 million before we had a single paying customer, and it made…
2. 72.57 (loss, 35.0s): Today my guest built a company, lost almost everything, and came back. So, um,…
3. 70.77 (contrarian, 23.5s): I think most startups should never raise venture money. Honestly, the best companies I…
4. 69.53 (number, 51.0s): My co-founder read it, tore it in half, and said give me 90 days.…
5. 66.55 (loss, 22.5s): I almost quit the company in 2021. I had my resignation letter written. My…

Top 5 on `podcast`:

1. 74.29 (contrarian, 21.0s): I think most startups should never raise venture money. Honestly, the best companies I…
2. 72.57 (loss, 34.4s): Today my guest built a company, lost almost everything, and came back. So, um,…
3. 72.19 (number, 26.5s): We raised $2 million before we had a single paying customer, and it made…
4. 69.35 (loss, 17.5s): Was there a moment you almost quit? I almost quit the company in 2021.…
5. 67.07 (number, 50.3s): My co-founder read it, tore it in half, and said give me 90 days.…

Top 5 on `multi`:

1. 74.71 (loss, 35.9s): I almost quit the company in 2021. I had my resignation letter written. My…
2. 74.66 (contrarian, 51.1s): I think most startups should never raise venture money. Honestly, the best companies I…
3. 72.57 (loss, 34.4s): Today my guest built a company, lost almost everything, and came back. So, um,…
4. 72.19 (number, 26.5s): We raised $2 million before we had a single paying customer, and it made…
5. 69.35 (loss, 17.5s): Was there a moment you almost quit? I almost quit the company in 2021.…

Top 5 on `scenes`:

1. 74.3 (contrarian, 21.0s): I think most startups should never raise venture money. Honestly, the best companies I…
2. 72.26 (loss, 34.4s): Today my guest built a company, lost almost everything, and came back. So, um,…
3. 71.79 (number, 26.5s): We raised $2 million before we had a single paying customer, and it made…
4. 69.36 (loss, 17.5s): Was there a moment you almost quit? I almost quit the company in 2021.…
5. 66.8 (number, 50.3s): My co-founder read it, tore it in half, and said give me 90 days.…

## Renders

| fixture | clip | layout | dur s | render s | A/V diff s | LUFS | dBTP | captions over speech | 1st caption − speech s | silence removed s | crop moves/min | track on face | split err | all checks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| solo | 1 | track | 25.55 | 7.5 | 0.015 | -14.1 | -4.3 | 1 | -0.023 | 1.2 | 0 | 1 | – | yes |
| solo | 2 | track | 32.91 | 8.9 | 0.008 | -14.2 | -4.4 | 1 | 0.132 | 2.1 | 0 | 1 | – | yes |
| solo | 3 | track | 22.05 | 6.8 | 0.013 | -14.1 | -4.3 | 1 | -0.051 | 1.67 | 0 | 1 | – | yes |
| podcast | 1 | split | 19.71 | 7.4 | 0.014 | -14 | -4.4 | 1 | -0.078 | 1.48 | 0 | – | 0.015 | yes |
| podcast | 2 | split | 32.45 | 10.9 | 0.013 | -14.1 | -4.1 | 1 | -0.02 | 2.04 | 0 | – | 0.015 | yes |
| podcast | 3 | split | 25.37 | 8.9 | 0.013 | -14.1 | -4.4 | 1 | 0.138 | 1.24 | 0 | – | 0.015 | yes |
| multi | 1 | blur | 35.08 | 21.5 | 0.008 | -14 | -4.3 | 0.917 | -0.087 | 0.92 | 0 | – | 0.013 | yes |
| multi | 2 | blur | 48.2 | 27 | 0.009 | -14 | -4.4 | 1 | -0.225 | 3.14 | 0 | – | 0.013 | yes |
| multi | 3 | blur | 32.45 | 21.1 | 0.013 | -14.1 | -4.1 | 1 | -0.02 | 2.04 | 0 | – | 0.013 | yes |
| scenes | 1 | mixed:split,track | 19.71 | 8.7 | 0.014 | -14 | -4.3 | 1 | -0.078 | 1.48 | 0 | 1 | 0.015 | yes |
| scenes | 2 | mixed:blur,split,track | 32.45 | 28.9 | 0.013 | -14.1 | -4.1 | 1 | -0.02 | 2.04 | 1.85 | 1 | 0.015 | yes |
| scenes | 3 | mixed:blur,split,track | 25.37 | 23 | 0.013 | -14.1 | -4.4 | 1 | 0.138 | 1.24 | 0 | 1 | 0.016 | yes |

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
