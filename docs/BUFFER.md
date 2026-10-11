# Publishing through Buffer

Buffer handles social account OAuth and platform developer integration. Ezra uses Buffer's
GraphQL API with a personal API key. No Buffer SDK or paid model API is required.

## Connect accounts

1. Sign in at https://publish.buffer.com and connect the social accounts you want to use.
2. In Buffer's API settings, generate a personal API key. Free accounts currently include one
   key and three channels. The free queue holds ten scheduled posts per channel at a time;
   published posts free up slots. Platform eligibility and Buffer plan limits still apply.
3. Initialize `EZRA_SECRET_KEY` in your local `.env` if needed (a long random value). Keep it
   backed up: it encrypts your stored credentials. `.env` is gitignored.
4. In Ezra's **Integrations → Buffer** panel, paste the key into the password field and choose
   **Save API key**. The authenticated server encrypts it locally; it is never returned to the UI.
   Alternatively, run `uv run ezra secrets set buffer` and enter it at the hidden prompt, or set
   `BUFFER_API_KEY` in the environment / `.env`. Environment values take precedence over stored keys.
5. Choose **Find Buffer accounts**, select accounts, and **Connect selected accounts**.

CLI equivalents:

```bash
uv run ezra accounts buffer-channels
uv run ezra accounts buffer-connect --tz America/Toronto
# Or select specific discovered IDs (repeat --channel for multiple accounts):
uv run ezra accounts buffer-connect --channel CHANNEL_ID --tz America/Toronto
uv run ezra accounts list
uv run ezra accounts health ACCOUNT_ID --live
```

The API offers `GET /api/integrations/buffer/channels` and
`POST /api/integrations/buffer/connect` with `{"channel_ids":["CHANNEL_ID"],"timezone":"America/Toronto"}`.
Both require Ezra's API authentication. MCP exposes `ezra_buffer_channels` and `ezra_connect_buffer`.
Credentials must be entered locally, not through an agent conversation.

Importing channels does not publish anything. Locked or disconnected channels are skipped;
paused queues must be resumed in Buffer and re-imported before publishing.

## Configure video hosting

Buffer does not accept binary video uploads. It fetches a public HTTPS video URL when publishing,
so media must remain available until delivery completes. Ezra's dashboard links and one-hour S3
presigned URLs are unsuitable for this. Ezra never uploads footage during previews or connection.

Choose one of these hosting options:

### Cloudinary (works with local Ezra)

Create a Cloudinary account and get the cloud name, API key and API secret from its dashboard.
In Ezra's **Integrations → Buffer → Video hosting**, enter these values and choose **Save video hosting**.
Alternatively, store this JSON using `uv run ezra secrets set cloudinary` at the hidden prompt:

```json
{"cloud_name":"YOUR_CLOUD","api_key":"YOUR_KEY","api_secret":"YOUR_SECRET"}
```

Alternatively set `EZRA_CLOUDINARY_JSON` to that JSON in `.env` or the environment. Cloudinary's
own storage, bandwidth and upload-size limits apply; check your plan before increasing volume.
Ezra uploads the approved rendered MP4 during confirmed publishing and reuses its stable URL
across retries. Media objects use content hashes under `ezra-buffer/`; sources are never uploaded.
Leave these objects available until Buffer confirms delivery. Cleanup is manual in Cloudinary.

### Existing S3-compatible storage

Configure `EZRA_STORAGE=s3` and its normal bucket credentials. Set
`EZRA_BUFFER_MEDIA_BASE_URL=https://media.example.com` to your bucket's public HTTPS base.
Allow public reads **only for `buffer-media/`**, keeping original sources and other objects private.
Ezra stages a copy of the approved render under `buffer-media/<sha256>.mp4` during confirmed
publishing. It does not change bucket permissions. Verify your base serves this prefix directly
without authentication. Keep the objects available until delivery; cleanup is manual.

## Publish and verify

Approve a rendered clip in Ezra, then preview the publishing plan:

```bash
uv run ezra publish --clip CLIP_ID --platforms youtube --dry-run
uv run ezra publish --clip CLIP_ID --platforms youtube
# Or specify an exact time; Buffer handles delivery even when Ezra is offline:
uv run ezra publish --clip CLIP_ID --platforms youtube --schedule 2026-12-01T09:00:00 --tz America/Toronto
```

The second command shows the plan and asks for confirmation. Missing credentials, missing hosting,
unapproved clips, FAIL compliance, and duplicates block publishing. Publishing stays a dry run in
the API/MCP unless `confirm=true`. Buffer scheduling uses `automatic`, never notification reminders.

`shareNow` is used for immediate posts; `customScheduled` with a UTC timestamp is used for schedules.
Ezra supports YouTube public/private/unlisted visibility through Buffer. Other Buffer destinations
are public-only. Instagram and Facebook videos are submitted as Reels. Captions, descriptions,
titles and required hashtags are preserved; platform validation can still refuse invalid content.

For Instagram and TikTok, a clip version's `spec.thumbnail_offset_ms` selects its cover frame
from the video (milliseconds). Buffer rejects separate video thumbnail image URLs. YouTube
Shorts custom covers must be set separately in YouTube Studio; Buffer cannot apply them.

Use `uv run ezra post refresh` (or MCP `ezra_refresh_posts`) to check delivery. With a running worker,
due Buffer schedules are polled automatically. A queued/processing post is not evidence of delivery:
Ezra records `published` only when Buffer reports `sent`, with the platform URL when available.
Metrics sync uses Buffer's normalized metrics; absent metrics stay unknown, and data can lag the network.

Automatic Buffer status checks run at most every 15 minutes while posts await delivery; manual
refreshes run immediately. Buffer Free currently allows 250 API requests per day and 3,000 per
30 days, shared across channels. Discovery, publishing, status checks and metrics all use this
allowance. For larger libraries, increase `EZRA_METRICS_SYNC_HOURS` and sync only relevant campaigns.
Ezra honors Buffer's `Retry-After` response when a job encounters an API rate limit.

`ezra post cancel POST_ID` removes a remote queued Buffer post before marking it cancelled locally.
`ezra post reschedule POST_ID TIMESTAMP` updates Buffer before the local schedule. If Buffer refuses
either operation, Ezra keeps its local record unchanged.

Buffer's `createPost` has no documented idempotency key. Ezra saves the returned post ID before
continuing, and retries with that ID only check status. If a timeout, server error or interrupted
process leaves the result unknown, Ezra refuses another submission (`submission_unknown`). Inspect
the Buffer queue before taking further action; do not create another post blindly. An explicit rate
limit rejection can retry normally. Failed or manually blocked Buffer posts never count as published.

References: [API](https://developers.buffer.com/reference.html),
[media hosting](https://developers.buffer.com/guides/hosting-media.html),
[pricing](https://buffer.com/pricing).
See also [API request limits](https://developers.buffer.com/guides/api-limits.html).
