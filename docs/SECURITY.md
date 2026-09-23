# Security

## Threat model

Ezra holds platform credentials that can post to a creator's accounts, and processes media files
that users upload. The main risks:
- posting something nobody approved, or posting from stolen tokens;
- leaking secrets through logs, tables or the browser;
- abusing file handling (path traversal, hostile uploads);
- CSRF / open redirects in the OAuth flow;
- publishing content without the right to use it.

## Controls

### Human approval and publishing

- Only `review.approve` moves a clip to `approved`. It refuses FAIL-compliance clips, and
  approving a REVIEW_REQUIRED clip records the acknowledged reasons in the audit log.
- Publishing requires an approved clip, is a dry run until `confirm=true`, and re-checks
  publish-stage compliance, posting limits, the campaign window and duplicates. The duplicate check
  uses a unique idempotency key per clip version, platform and account, so a retried request can
  never double-post.
- Autonomous publishing needs two independent opt-ins: the campaign's
  `human_approval_required: false` and `EZRA_ALLOW_AUTONOMOUS=1` in the environment. Even then it
  only publishes PASS clips, never REVIEW_REQUIRED.
- MCP: tool descriptions and server instructions tell the agent to ask. Claude Code asks the user
  before each tool call unless the user pre-allowed it.
- Publishing uses official platform APIs, an aggregator account you hold, or local export. There's
  no password handling and no browser automation.

### Secrets (`secrets.py`)

- Tables never hold secrets. Accounts and credentials store a `credential_ref` / `secret_ref`, and
  the value lives in:
  1. the environment (`EZRA_SECRET_<REF>` or the provider's conventional variable, e.g.
     `UPLOAD_POST_API_KEY`); or
  2. `$EZRA_HOME/secrets.enc`, a Fernet file (AES-128-CBC + HMAC-SHA256) keyed by a SHA-256 of
     `EZRA_SECRET_KEY`, written atomically with mode 0600.
- Without `EZRA_SECRET_KEY` the file store is unavailable, and errors say so. Missing credentials
  produce errors that name the variable to set, never the value.
- `ezra secrets list`, `/api/integrations` and `/api/accounts` show only names and
  presence/absence.
- OAuth tokens (access and refresh) go straight into the secret store. Refresh happens in
  `Publisher.token()`, and refreshed tokens are written back.

### API

- Bearer token (`EZRA_API_TOKEN`), compared in constant time. Unauthenticated routes are only
  `/api/health`, signed media links and the OAuth callback (which is protected by its state).
- The dashboard never exposes the token: the browser calls the Next.js server route
  `/api/ezra/*`, which adds it. That route rejects `..` and backslashes in paths.
- CORS allows only `EZRA_WEB_URL`. Security headers are `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` and `Referrer-Policy: same-origin`. There's a per-client rate limit
  (`EZRA_RATE_LIMIT_PER_MINUTE`, answered with 429).
- Media links are HMAC-signed (`EZRA_SECRET_KEY`, falling back to the API token) with an expiry
  (6 h). A tampered or expired link gets 403.
- Errors map to status codes with a message; tracebacks stay in job records and server logs.

### Files and media

- **Uploads**:
  - extension allowlist (video/audio, images for brand kits);
  - size limit (`EZRA_MAX_UPLOAD_MB`) enforced while streaming, so the whole file is never held
    in memory;
  - ffprobe must parse the file and find the expected streams.
- **Storage keys** are validated (`storage.validate_key`): no absolute paths, no `..`, no
  backslashes or NUL. Local storage also checks that the resolved path stays inside the storage
  root.
- **Server-side imports** (API `sources/import`, MCP `ezra_add_source`) must resolve inside
  `EZRA_IMPORT_ROOTS` (default: the working directory and `EZRA_HOME`), with symlinks resolved.
- External tools (ffmpeg, ffprobe, yt-dlp, claude, codex) run with argument lists, never a shell.
- Each job runs in its own child process, so a crashing decoder can't take the worker down.

### OAuth

- Authorization code flow with **PKCE (S256)** for every provider. `state` is random, single-use,
  bound to the provider, and expires after 10 minutes (`oauth_states` table).
- The redirect URI is derived from `EZRA_PUBLIC_URL`. After the callback, redirects only go to
  the dashboard origin (`safe_redirect` blocks open redirects).
- Requested scopes are the minimum each platform needs to upload and read metrics (see
  REMAINING_EXTERNAL_SETUP.md).

### Content rights

- Every source has `rights_status` (authorized / unverified / rejected) and a `rights_basis`
  (campaign_supplied, owned, licensed, permission, unknown). Changes are audited.
- The `source_rights` rule makes candidates from unverified sources REVIEW_REQUIRED and from
  rejected sources FAIL, so they can't be approved or published.
- Ezra doesn't scrape marketplaces or download footage on its own. URL ingest (yt-dlp) happens
  only when a user asks for it and still requires a rights basis.
- B-roll comes only from your own library, Pexels (licensed for this use; attribution kept in
  metadata) or generated text cards.

### Audit log

`audit_events` records who did what:
- source ingests and rights changes;
- renders, clip edits, approvals (with acknowledged review reasons), rejections and exports;
- posts created, published, failed, retried and cancelled;
- account connections, secrets stored with `ezra secrets set` (the name only);
- weight updates and autonomous runs.

View it with `ezra audit-log` or `GET /api/audit`.

## Not included

- **Multi-user accounts / roles.** One API token grants full access. Put the dashboard behind your
  own SSO / reverse-proxy auth for teams.
- **Inbound webhooks.** `security.sign` / `security.verify` implement timestamped HMAC signatures
  (`t=…,v1=…`, 5-minute tolerance), but no route accepts webhooks yet.
- **Virus scanning of uploads.** Files are parsed by ffprobe/ffmpeg only; run Ezra's workers
  with least privilege (the Docker image uses a non-root user).

## Reporting

Open a private security advisory on the GitHub repository.
