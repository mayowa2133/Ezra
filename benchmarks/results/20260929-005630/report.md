# Ezra benchmark: 2026-09-29T00:56:30-04:00

Machine: macOS-26.6.2-arm64-arm-64bit, Python 3.11.6, 10 CPUs. Total 311.9s.

Providers: whisper `small`, diarizer `local`, faces `auto`, scoring heuristic (no model).

## Speed

| fixture | duration s | analyze s | × realtime | candidates s | render top 3 s |
|---|---|---|---|---|---|
| solo | 182.154 | 34 | 0.187 | 0.2 | 35.1 |
| podcast | 179.001 | 32.3 | 0.18 | 0.2 | 35.3 |
| multi | 201.788 | 39.1 | 0.194 | 0.2 | 44.8 |
| scenes | 179.001 | 34.3 | 0.191 | 0.2 | 46.1 |

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
| solo | 14 | 1 | 1 | 0 | 1 | 0.333 | yes | 1 | 0 |
| podcast | 15 | 1 | 1 | 0 | 1 | 0.5 | yes | 0 | 0.008 |
| multi | 18 | 1 | 1 | 0 | 1 | 0.5 | yes | 1 | 0.012 |
| scenes | 15 | 1 | 1 | 0 | 1 | 0.5 | yes | 0 | 0.008 |

Top 5 on `solo`:

1. 77.71 (number, 26.6s): We raised $2 million before we had a single paying customer, and it made…
2. 74.22 (loss, 35.0s): Today my guest built a company, lost almost everything, and came back. So, um,…
3. 67.87 (loss, 22.5s): I almost quit the company in 2021. I had my resignation letter written. My…
4. 61.51 (question, 21.2s): If you started over tomorrow with $0, what would you do first? I would…
5. 59.86 (contrarian, 23.4s): Controversial, why do you think people disagree? Because the stories we celebrate are the…

Top 5 on `podcast`:

1. 76.93 (contrarian, 21.0s): I think most startups should never raise venture money. Honestly, the best companies I…
2. 75.65 (loss, 34.4s): Today my guest built a company, lost almost everything, and came back. So, um,…
3. 73.07 (loss, 24.8s): What does the biggest mistake founders make with money? They raised too early. We…
4. 69.49 (loss, 17.5s): Was there a moment you almost quit? I almost quit the company in 2021.…
5. 67.06 (question, 15.6s): If you started over tomorrow with $0, what would you do first? I would…

Top 5 on `multi`:

1. 77.32 (contrarian, 51.1s): I think most startups should never raise venture money. Honestly, the best companies I…
2. 75.67 (loss, 34.4s): Today my guest built a company, lost almost everything, and came back. So, um,…
3. 73.15 (loss, 35.9s): I almost quit the company in 2021. I had my resignation letter written. My…
4. 73.05 (loss, 24.7s): What is the biggest mistake founders make with money? They raised too early. We…
5. 66.3 (other, 21.7s): Venture money built the company I work at, and it changed my life. That…

Top 5 on `scenes`:

1. 76.94 (contrarian, 21.0s): I think most startups should never raise venture money. Honestly, the best companies I…
2. 75.07 (loss, 34.4s): Today my guest built a company, lost almost everything, and came back. So, um,…
3. 71.7 (loss, 24.8s): What does the biggest mistake founders make with money? They raised too early. We…
4. 70.81 (loss, 17.5s): Was there a moment you almost quit? I almost quit the company in 2021.…
5. 67.06 (question, 15.6s): If you started over tomorrow with $0, what would you do first? I would…

## Renders

| fixture | clip | layout | dur s | render s | A/V diff s | LUFS | dBTP | captions over speech | 1st caption − speech s | silence removed s | crop moves/min | track on face | split err | all checks |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| solo | 1 | track | 26.13 | 11.2 | 0.003 | -14.1 | -4.3 | 0.933 | -0.023 | 0.29 | 0 | 1 | – | yes |
| solo | 2 | track | 33.77 | 14.3 | 0.008 | -14.2 | -4.3 | 1 | 0.132 | 0.92 | 0 | 1 | – | yes |
| solo | 3 | track | 22.5 | 9.3 | 0.03 | -14.3 | -4.4 | 0.933 | -0.074 | 0 | 0 | 1 | – | yes |
| podcast | 1 | split | 21 | 9.3 | 0 | -14.1 | -4.2 | 1 | -0.078 | 0 | 0 | – | 0.015 | yes |
| podcast | 2 | split | 33.61 | 14.5 | 0.008 | -14.1 | -4.4 | 1 | -0.021 | 0.54 | 0 | – | 0.015 | yes |
| podcast | 3 | split | 24.24 | 11.2 | 0.009 | -14 | -4.4 | 1 | -0.008 | 0.25 | 0 | – | 0.015 | yes |
| multi | 1 | track | 51.07 | 18.4 | 0.006 | -14 | -4.4 | 1 | -0.225 | 0 | 0 | 1 | 0.013 | yes |
| multi | 2 | track | 33.61 | 13.2 | 0.008 | -14.1 | -4.4 | 1 | -0.021 | 0.54 | 1.79 | 1 | 0.013 | yes |
| multi | 3 | track | 35.94 | 12.9 | 0.007 | -14 | -4.3 | 0.917 | -0.087 | 0 | 0 | 1 | 0.013 | yes |
| scenes | 1 | mixed:split,track | 21 | 10.2 | 0 | -14.1 | -4.2 | 1 | -0.078 | 0 | 0 | 1 | 0.015 | yes |
| scenes | 2 | mixed:blur,split,track | 33.61 | 19.5 | 0.008 | -14.1 | -4.4 | 1 | -0.021 | 0.54 | 1.79 | 1 | 0.015 | yes |
| scenes | 3 | mixed:blur,split,track | 24.24 | 16.1 | 0.009 | -14 | -4.5 | 1 | -0.008 | 0.25 | 0 | 1 | 0.015 | yes |

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
