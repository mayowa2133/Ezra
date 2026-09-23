# Remaining external setup

Everything below needs **your** accounts, apps or approvals. Ezra is complete up to each credential
boundary: without a credential, the feature fails with a message naming the variable to set, and
the rest of the system keeps working (local export, manual metrics, heuristic scoring).

Credentials go in the environment (`.env`) or the encrypted store (`ezra secrets set <ref>`, which
needs `EZRA_SECRET_KEY`). Check what's configured with `ezra accounts list`, `GET /api/integrations`
or the dashboard's Integrations page.

## 0. Required for any shared deployment

| Variable | What | How |
|---|---|---|
| `EZRA_API_TOKEN` | Bearer token for the API (the dashboard proxy sends it) | Any long random string |
| `EZRA_SECRET_KEY` | Encrypts stored OAuth tokens and keys; signs media links | `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Back it up: losing it means reconnecting accounts |
| `EZRA_PUBLIC_URL` | Where browsers and OAuth providers reach the API | e.g. `https://ezra-api.example.com` (OAuth redirect URIs are built from it) |
| `EZRA_WEB_URL` | Dashboard origin (CORS, post-OAuth redirect) | e.g. `https://ezra.example.com` |

OAuth callbacks go to `{EZRA_PUBLIC_URL}/api/integrations/{provider}/callback`, so the API must be
running and reachable there when you connect an account.

## 1. YouTube (YouTube Data API v3)

1. In Google Cloud Console, create a project and enable **YouTube Data API v3**.
2. Configure the **OAuth consent screen** (External). While the app is in testing, add your Google
   account as a test user.
3. Create an **OAuth client ID** of type *Web application* with authorized redirect URI
   `{EZRA_PUBLIC_URL}/api/integrations/youtube/callback`.
4. Put the downloaded JSON in `YOUTUBE_CLIENT_SECRET_JSON`. Google's `{"web": {...}}` file works
   as-is, as does `{"client_id": "...", "client_secret": "..."}`.
5. `ezra accounts connect youtube`, open the printed URL, and approve. The channel becomes an
   account with its tokens in the secret store.

Scopes requested: `youtube.upload`, `youtube.readonly` (channel lookup and view counts).

Platform constraints to know:
- **Unverified API projects created after 28 July 2020 can only upload private videos** until
  the project passes YouTube's API Services audit (compliance review). Apply once you're ready to
  post publicly.
- Uploads consume daily API quota (default 10,000 units per project; an upload is expensive), so
  request a quota increase for volume.
- Scheduling uses YouTube's own `publishAt` (the video uploads private and goes public at that time).

## 2. TikTok (Content Posting API)

1. At developers.tiktok.com, create an app. Add **Login Kit** and the **Content Posting API**
   (Direct Post) products.
2. Register the redirect URI `{EZRA_PUBLIC_URL}/api/integrations/tiktok/callback` and request the
   scopes `user.info.basic`, `video.publish` and `video.list`.
3. Set `TIKTOK_CLIENT_JSON={"client_id": "<client key>", "client_secret": "<client secret>"}`.
4. `ezra accounts connect tiktok` and approve.

Platform constraints:
- **Unaudited apps can only post privately (SELF_ONLY)**, and only for a small number of users.
  Ezra posts SELF_ONLY until you mark the account audited. After TikTok approves your app's audit,
  run `ezra accounts add tiktok tiktok <handle> --audited` to allow public posts.
- Uploads use FILE_UPLOAD with chunked PUTs (no public video URL needed).

## 3. Instagram (Instagram API with Instagram Login)

1. At developers.facebook.com, create an app, add the **Instagram** product, and use *API setup
   with Instagram login*.
2. The Instagram account must be a **professional (Business or Creator)** account.
3. Add the redirect URI `{EZRA_PUBLIC_URL}/api/integrations/instagram/callback`. Permissions:
   `instagram_business_basic`, `instagram_business_content_publish`,
   `instagram_business_manage_insights`.
4. Set `INSTAGRAM_CLIENT_JSON={"client_id": "<Instagram app ID>", "client_secret": "<Instagram app secret>"}`.
5. `ezra accounts connect instagram` and approve.

Platform constraints:
- Posting to accounts other than the app's own roles needs **Advanced Access** through Meta App
  Review.
- **100 API-published posts per account per rolling 24 hours.**
- Reels are always public: Instagram has no private posts, and Ezra refuses `--private` there.
- Uploads use the resumable `rupload` endpoint (no public video URL needed).

## 4. Upload-Post (optional aggregator)

Instead of your own platform apps, you can post through https://upload-post.com:

1. Create an account and a **profile**, and connect TikTok/Instagram/YouTube to it in their
   dashboard.
2. Set `UPLOAD_POST_API_KEY`.
3. Register accounts with the profile name as the handle:
   `ezra accounts add tiktok upload-post <profile>` (same for instagram/youtube).

Their plan limits apply (the free plan has a small monthly upload quota, and some platforms need a
paid tier).

## 5. Optional providers

| Feature | Needs | Setup |
|---|---|---|
| pyannote diarization (better on similar voices / 3+ speakers) | Hugging Face token | Accept the terms of `pyannote/speaker-diarization-community-1` on huggingface.co; `HF_TOKEN=...`; `uv pip install pyannote.audio`; `EZRA_DIARIZER=pyannote` |
| WhisperX alignment | – | `uv pip install whisperx`; `EZRA_TRANSCRIBER=whisperx` |
| Stock B-roll | Pexels API key (free) | `PEXELS_API_KEY=...`, or `EZRA_BROLL_LIBRARY=/path` for your own licensed clips |
| Model critique | Claude Code or Codex logged in, or a local model server | `EZRA_LLM=claude-cli` (run `claude` once and `/login`), `codex-cli`, or `openai-compatible` + `EZRA_LLM_BASE_URL` |
| OpenShorts moments | OpenShorts' own model: `GEMINI_API_KEY` or a local OpenAI-compatible server | `docker compose --profile openshorts up -d`; `EZRA_CLIP_ENGINE=openshorts`; `EZRA_OPENSHORTS_URL=http://localhost:8001` |
| GPU transcription | NVIDIA GPU + CUDA | `EZRA_WHISPER_DEVICE=cuda`, `EZRA_WHISPER_COMPUTE=float16` |

## 6. Campaign marketplaces

Ezra doesn't log into or scrape marketplaces (Content Rewards, Whop, …). Export or copy a
campaign's terms into a YAML/JSON/CSV file (see docs/CAMPAIGNS.md) and import it. Submitting posts
for payout and recording confirmed payouts (`POST /api/campaigns/{ref}/revenue` or the dashboard)
stay manual unless a marketplace offers an official API.

## What was verified without these credentials

- Every publisher adapter runs against mocked HTTP APIs that follow the documented request and
  response shapes: OAuth + PKCE, token refresh, resumable and chunked uploads, scheduling, status
  polling, metrics, rate-limit retries and error paths.
- Local export exercises the full publish pipeline (approval gate, dry run, idempotency, posting
  limits, jobs, metrics, earnings).
- No real post was made to any platform.
