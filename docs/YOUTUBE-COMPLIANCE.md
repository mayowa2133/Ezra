# YouTube API compliance

How Ezra uses the YouTube Data API v3 and the YouTube Analytics API, written for the person
deploying Ezra and for a YouTube API compliance audit. It describes the code as of 2026-09-27
(`src/ezra/publishing/youtube.py`, `youtube_analytics.py`, `publishing/__init__.py`).

Policy references are to Google's
[YouTube API Services Terms of Service](https://developers.google.com/youtube/terms/api-services-terms-of-service)
and [Developer Policies](https://developers.google.com/youtube/terms/developer-policies). Read the
current versions before an audit; this document describes Ezra's behaviour and isn't legal advice.

## What Ezra is

A tool one person or team runs for their own channels. It cuts short clips from long-form footage
they're authorized to use, and uploads the clips a human approved to their own YouTube channel.
It then reads those videos' statistics to learn which kinds of clips work.

## OAuth scopes

Requested at connect time (`ezra accounts connect youtube`, or the dashboard's Connect button):

| Scope | Why | Requested |
|---|---|---|
| `https://www.googleapis.com/auth/youtube.upload` | Upload approved clips; set their thumbnails | Always |
| `https://www.googleapis.com/auth/youtube.readonly` | Read the connected channel's id and name; read status and statistics of videos Ezra uploaded | Always |
| `https://www.googleapis.com/auth/yt-analytics.readonly` | Read watch time, average view percentage and subscriber changes for videos Ezra uploaded | Always |
| `https://www.googleapis.com/auth/youtube.force-ssl` | Edit, reschedule or delete a video Ezra uploaded | Only with `EZRA_YOUTUBE_MANAGE=1` |

Ezra asks for nothing else: no comments, likes, subscriptions, playlists, captions, channel
settings or content owner scopes.

## What Ezra calls

| Endpoint | When |
|---|---|
| `oauth2.googleapis.com/token` | Code exchange (PKCE) and access-token refresh |
| `oauth2.googleapis.com/revoke` | When the user disconnects the account |
| `youtube/v3/channels?mine=true` | At connect (channel id and name), and the health check |
| `upload/youtube/v3/videos` (resumable) | Uploading an approved clip, or the private connection test |
| `upload/youtube/v3/thumbnails/set` | Right after an upload, when the clip has a thumbnail |
| `youtube/v3/videos` (GET) | Status, processing state and statistics of videos Ezra uploaded |
| `youtube/v3/videos` (PUT/DELETE) | Only when the user asks to edit, reschedule, cancel or delete one of those videos (manage scope) |
| `youtubeanalytics.googleapis.com/v2/reports` | Per-video analytics for videos Ezra uploaded, filtered to their ids |

## What Ezra does not do

- It doesn't scrape YouTube or read other channels' data.
- It doesn't download audiovisual content through the API or any unofficial means. Source footage
  is supplied by the user with a recorded rights basis. The optional `yt-dlp` URL ingest is off the
  API path and documented as for footage the user is authorized to use.
- It doesn't like, comment, subscribe, view or otherwise generate engagement. It has no scopes that
  could.
- It doesn't publish without a human. Every clip needs explicit approval (`review.approve`), and
  every post a confirmed publish (`confirm=true`, a CLI prompt, or a dashboard button). Agents
  using the MCP tools are instructed to ask the human and can't bypass the gate.
- It doesn't hide publishing from the user. Every post is shown with its status, platform state,
  URL and errors in the dashboard, CLI and MCP, and every step is audit-logged.
- It doesn't request or store the user's Google password; sign-in happens on Google's consent page.

## Human approval and source rights

1. Footage is ingested with a rights basis (`owned`, `licensed`, `permission`, `campaign_supplied`).
   Unverified rights put every clip from it into REVIEW_REQUIRED, and rejected rights FAIL it, so
   it can't be approved.
2. Clips are rendered and must be approved by a person. Compliance failures can't be approved.
3. Publishing is a dry run that shows exactly what would be posted, where and how. It only goes out
   after the person confirms.
4. Autonomous publishing exists only when a campaign explicitly disables approval *and* the operator
   sets `EZRA_ALLOW_AUTONOMOUS=1`, and even then only for clips that pass every compliance check.

## Public uploads before the audit

Google restricts videos uploaded through unverified API projects (created after 28 July 2020) to
private viewing until the project passes a compliance audit. Ezra follows this by default:

- `EZRA_YOUTUBE_PUBLIC_ALLOWED` defaults to off. With it off, public and scheduled uploads are
  refused before anything is sent. The error explains why and suggests a private upload instead.
- If YouTube returns a video as private when public was requested, the post carries a
  `locked_private` warning. Ezra never reports a video as public when it isn't.
- Set `EZRA_YOUTUBE_PUBLIC_ALLOWED=1` only after the audit is approved.

## OAuth flow

- Authorization-code flow with PKCE (S256) and a single-use, provider-bound `state` (10-minute
  expiry). Offline access with `prompt=consent`, so Google returns a refresh token.
- The CLI uses a one-shot loopback listener on `127.0.0.1` (Google's recommended flow for installed
  apps; use a "Desktop app" OAuth client). The dashboard uses the API's callback (a "Web application"
  client with `<EZRA_PUBLIC_URL>/api/integrations/youtube/callback` registered).
- If Google returns no refresh token or the upload permission wasn't granted, the connection fails
  with a reason instead of half-working.

## Token storage

- Tokens are stored in Ezra's encrypted secret store (Fernet, keyed by `EZRA_SECRET_KEY`) under a
  reference such as `youtube:<channel name>`. Database tables hold only that reference.
- The OAuth client secret is stored the same way (`youtube-client`, or
  `YOUTUBE_CLIENT_SECRET_JSON` in the environment).
- Access tokens, refresh tokens and client secrets are never logged, never returned by the API,
  CLI or MCP (the health view shows only whether they exist, the granted scopes and the expiry),
  and never written to audit events.
- An expired access token is refreshed automatically. A revoked or expired refresh token
  (`invalid_grant`) marks the account `reconnect_required` and stops publishing to it.

## Disconnecting and deleting data

- `ezra accounts disconnect <id>` (or the dashboard) revokes the token at Google, deletes it from
  the secret store and marks the account disconnected.
- `--purge` (API: `?purge=true`) also deletes every metric snapshot and raw API response Ezra
  stored for that account's posts. Ezra's own records of what it posted (clip, title, time) remain,
  without YouTube API data.
- Users can also revoke Ezra's access at any time at https://myaccount.google.com/permissions;
  Ezra then asks them to reconnect.

## YouTube API data Ezra stores

| Data | Where | Why |
|---|---|---|
| Channel id and name of the connected channel | `publish_accounts` | To show which channel is connected and upload to it |
| Granted scopes, token expiry | `publish_accounts.meta`, `integration_credentials` | To show permission health |
| Video id, URL, privacy/upload status of Ezra's uploads | `posts` | To show the post and its state |
| Statistics (views, likes, comments) of Ezra's uploads | `metric_snapshots` (provider `youtube-api`) | Earnings and performance learning |
| Analytics (views, likes, comments, shares, watch minutes, average view duration and %, subscribers gained/lost) of Ezra's uploads | `metric_snapshots` (provider `youtube-analytics`), with window and raw row | Performance learning |

Snapshots are refreshed by the metrics sync (`EZRA_METRICS_SYNC_HOURS`, default 6 hours). The
Developer Policies set rules on how long API data may be stored without being refreshed, and on
deleting it when a user disconnects. Check the current wording for your deployment. `--purge`
covers deletion on request.

## Derived data

`ezra performance` and the learning layer compute cohort comparisons (shrunken means, intervals,
rank correlations) from the snapshots above, over the user's own videos only. Results are
presented as observational with N and a confidence level, and aren't shared outside the deployment.

## Privacy policy requirements for a deployment

Anyone deploying Ezra for users other than themselves needs, before the audit:
- a privacy policy describing the data above, how long it's kept and how to delete it;
- links to the [YouTube Terms of Service](https://www.youtube.com/t/terms) and the
  [Google Privacy Policy](https://policies.google.com/privacy) where users connect their account;
- an OAuth consent screen (published, not in Testing) with the scopes listed above.

A consent screen left in Testing issues refresh tokens that expire after 7 days. Ezra reports
that as `auth_revoked` and asks the user to reconnect.

## Quota

A resumable upload costs about 1,600 quota units of the default 10,000 a day, so about 6 uploads.
Thumbnails, updates and reads cost 50 or fewer each. `quotaExceeded` is permanent for the day:
Ezra records `quota_exceeded` on the post and doesn't retry until the user asks. It notes that the
quota resets at midnight Pacific time. Request a quota increase with the audit if you need more
daily uploads.
