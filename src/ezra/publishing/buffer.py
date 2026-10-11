"""Buffer's GraphQL API. Social OAuth is handled in Buffer, not by Ezra.

Docs: https://developers.buffer.com/reference.html
Media must have a stable public URL until delivery. Only confirmed publish jobs
stage media; previews and channel discovery never upload footage.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .. import secrets
from ..config import get_settings
from ..storage import get_storage, sha256_file
from .base import MetricsResult, PostRequest, Publisher, PublishError, PublishResult, RetryablePublishError

POST_FIELDS = "id status externalLink schedulingType error { message }"


def public_media_url(url: str) -> str:
    """Reject local, authenticated and expiring media links without fetching them."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    try:
        local_ip = not ip_address(host).is_global
    except ValueError:
        local_ip = False
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.query
            or parsed.fragment or local_ip or host == "localhost" or host.endswith(".local")):
        raise PublishError("Buffer needs a stable public HTTPS media URL without credentials or expiry parameters",
                           "media_not_public")
    return url


class BufferPublisher(Publisher):
    name = "buffer"
    platforms = ("tiktok", "instagram", "youtube", "facebook", "x", "linkedin", "threads", "bluesky")
    supports_scheduling = True
    api = "https://api.buffer.com"

    def _headers(self, account: dict[str, Any]) -> dict[str, str]:
        ref = account.get("credential_ref") or "buffer"
        try:
            key = secrets.require(ref, "Buffer publishing")
        except secrets.SecretError as e:
            raise PublishError(str(e), "credential_missing", reconnect=True) from e
        if not isinstance(key, str) or not key.strip():
            raise PublishError("Buffer API key must be a non-empty string", "credential_missing", reconnect=True)
        return {"Authorization": f"Bearer {key.strip()}"}

    def _graphql(self, account: dict[str, Any], query: str,
                 variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.http.post(self.api, headers=self._headers(account),
                                  json={"query": query, "variables": variables or {}})
        if response.status_code in (401, 403):
            raise PublishError("Buffer access denied; check the API key and channel permissions",
                               "not_connected", reconnect=True)
        # Do not put response bodies in errors: they can echo submitted content or credentials.
        if response.status_code == 429 or response.status_code >= 500:
            retry = response.headers.get("retry-after", "")
            raise RetryablePublishError(f"Buffer HTTP {response.status_code}",
                                        "rate_limited" if response.status_code == 429 else "server_error",
                                        retry_after=int(retry) if retry.isdigit() else None)
        if response.status_code >= 400:
            raise PublishError(f"Buffer HTTP {response.status_code}", "http_error")
        try:
            body = response.json()
        except ValueError as e:
            raise PublishError("Buffer returned an invalid response", "invalid_response") from e
        if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
            raise PublishError("Buffer GraphQL request failed; check API permissions and configuration",
                               "graphql_error")
        return body["data"]

    def channels(self, credential_ref: str = "buffer") -> list[dict[str, Any]]:
        account = {"credential_ref": credential_ref}
        data = self._graphql(account, "query { account { organizations { id name } } }")
        out = []
        for org in data["account"]["organizations"]:
            data = self._graphql(account, """query EzraChannels($input: ChannelsInput!) {
                channels(input: $input) {
                    id name displayName service isDisconnected isLocked isQueuePaused
                }
            }""", {"input": {"organizationId": org["id"]}})
            for channel in data["channels"]:
                platform = {"twitter": "x"}.get(channel["service"], channel["service"])
                if platform in self.platforms:
                    out.append({"channel_id": channel["id"], "platform": platform,
                                "handle": channel.get("name") or channel.get("displayName") or channel["id"],
                                "organization_id": org["id"], "organization": org["name"],
                                "connected": not channel["isDisconnected"], "locked": channel["isLocked"],
                                "queue_paused": channel["isQueuePaused"]})
        return out

    def preflight(self, account: dict[str, Any], visibility: str) -> None:
        self._headers(account)
        platform = account.get("platform")
        if visibility != "public" and platform != "youtube":
            raise PublishError("Buffer private/unlisted publishing is supported only for YouTube", "visibility")
        if not account.get("meta", {}).get("channel_id"):
            raise PublishError("Import a Buffer channel with ezra accounts buffer-connect", "no_channel")
        if account.get("meta", {}).get("locked") or account.get("meta", {}).get("queue_paused"):
            raise PublishError("Buffer channel is locked or its queue is paused; fix it in Buffer and reconnect",
                               "channel_unavailable")
        base = get_settings().buffer_media_base_url
        if base:
            public_media_url(base)
            if get_settings().storage != "s3":
                raise PublishError("EZRA_BUFFER_MEDIA_BASE_URL requires S3 storage with a public media prefix",
                                   "media_not_configured")
        else:
            cloud = secrets.get("cloudinary")
            if not isinstance(cloud, dict) or not all(cloud.get(k) for k in ("cloud_name", "api_key", "api_secret")):
                raise PublishError("Buffer needs media hosting: set EZRA_CLOUDINARY_JSON with cloud_name, api_key, "
                                   "api_secret, or configure S3 + EZRA_BUFFER_MEDIA_BASE_URL (docs/BUFFER.md)",
                                   "media_not_configured")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(cloud["cloud_name"])):
                raise PublishError("Invalid Cloudinary cloud_name", "media_not_configured")

    def _media(self, req: PostRequest) -> str:
        digest = sha256_file(req.video)
        base = get_settings().buffer_media_base_url
        if base:
            key = f"buffer-media/{digest}.mp4"
            storage = get_storage()
            if not storage.exists(key):
                storage.put_file(key, req.video, "video/mp4")
            return public_media_url(f"{base.rstrip('/')}/{quote(key, safe='/')}")
        cloud = secrets.require("cloudinary", "Buffer media hosting")
        # Stable, content-addressed objects make re-uploading after a lost response safe.
        with req.video.open("rb") as video:
            response = self.http.post(f"https://api.cloudinary.com/v1_1/{cloud['cloud_name']}/video/upload",
                                      auth=(str(cloud["api_key"]), str(cloud["api_secret"])),
                                      data={"public_id": f"ezra-buffer/{digest}", "overwrite": "false"},
                                      files={"file": ("clip.mp4", video, "video/mp4")})
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryablePublishError("Cloudinary media upload temporarily unavailable", "media_upload")
        if response.status_code >= 400:
            raise PublishError(f"Cloudinary upload failed (HTTP {response.status_code}); check hosting limits",
                               "media_upload")
        try:
            return public_media_url(response.json()["secure_url"])
        except (ValueError, KeyError, TypeError) as e:
            raise PublishError("Cloudinary returned no public media URL", "media_upload") from e

    @staticmethod
    def _result(post: dict[str, Any]) -> PublishResult:
        status = post.get("status")
        if status == "error":
            raise PublishError("Buffer could not publish the post; inspect its queue for details", "delivery_failed")
        if post.get("schedulingType") != "automatic":
            raise PublishError("Buffer requires manual notification publishing for this post", "manual_publishing")
        if status not in ("sent", "scheduled", "sending"):
            raise PublishError(f"Buffer post needs attention (status: {status})", "delivery_blocked")
        sent = status == "sent"
        return PublishResult("published" if sent else "scheduled" if status == "scheduled" else "processing",
                             post["id"], post.get("externalLink"), post,
                             platform_state="public" if sent else status)

    def refresh_status(self, account: dict[str, Any], external_id: str) -> PublishResult:
        query = f"query EzraPost($input: PostInput!) {{ post(input: $input) {{ {POST_FIELDS} }} }}"
        data = self._graphql(account, query,
                             {"input": {"id": external_id}})
        result = self._result(data["post"])
        if result.status == "published" and account.get("platform") == "youtube":
            # The service preserves the requested visibility; status alone does not prove public delivery.
            result.platform_state = account.get("_visibility", "public")
        return result

    def publish(self, account: dict[str, Any], req: PostRequest, platform: str) -> PublishResult:
        state = dict(req.upload_state)
        if state.get("buffer_post_id"):
            return self.refresh_status(account | {"_visibility": req.visibility}, state["buffer_post_id"])
        if state.get("buffer_submission_pending"):
            raise PublishError("A previous Buffer submission may have succeeded. Check the Buffer queue before "
                               "retrying; Ezra will not create a possible duplicate", "submission_unknown")
        self.preflight(account | {"platform": platform}, req.visibility)
        if req.thumbnail_offset_ms is not None and (
                type(req.thumbnail_offset_ms) is not int or req.thumbnail_offset_ms < 0):
            raise PublishError("Thumbnail frame offset must be a non-negative integer in milliseconds",
                               "thumbnail_offset")

        def save(**values: Any) -> None:
            state.update(values)
            if req.on_state:
                req.on_state(dict(state))

        url = state.get("buffer_media_url") or self._media(req)
        save(buffer_media_url=url)
        text = req.caption
        if platform == "youtube" and req.description:
            text = req.description if req.caption in req.description else f"{req.description}\n\n{req.caption}"
        # Required campaign tags must survive even when a YouTube description is supplied.
        missing = [f"#{tag.lstrip('#')}" for tag in req.hashtags if f"#{tag.lstrip('#')}" not in text]
        if missing:
            text += "\n\n" + " ".join(missing)
        metadata: dict[str, Any] = {}
        if platform == "youtube":
            metadata["youtube"] = {"title": req.title or req.caption, "categoryId": "22", "privacy": req.visibility}
        elif platform == "instagram":
            metadata["instagram"] = {"type": "reel", "shouldShareToFeed": True}
        elif platform == "facebook":
            metadata["facebook"] = {"type": "reel"}
        video_asset: dict[str, Any] = {"url": url}
        if req.thumbnail_offset_ms is not None and platform in ("instagram", "tiktok"):
            video_asset["metadata"] = {"thumbnailOffset": req.thumbnail_offset_ms}
        payload: dict[str, Any] = {"channelId": account["meta"]["channel_id"], "text": text,
                                   "schedulingType": "automatic", "mode": "shareNow", "saveToDraft": False,
                                   "needsApproval": False, "assets": [{"video": video_asset}], "metadata": metadata}
        if req.scheduled_at:
            payload.update(mode="customScheduled", dueAt=req.scheduled_at.astimezone(UTC).isoformat())
        # createPost has no documented idempotency key. Persist uncertainty BEFORE sending.
        save(buffer_submission_pending=True)
        try:
            data = self._graphql(account, f"""mutation EzraPublish($input: CreatePostInput!) {{
                createPost(input: $input) {{
                    __typename ... on PostActionSuccess {{ post {{ {POST_FIELDS} }} }}
                    ... on MutationError {{ message }}
                }}
            }}""", {"input": payload})
        except RetryablePublishError as e:
            if e.code == "rate_limited":
                save(buffer_submission_pending=False)
                raise
            raise PublishError("Buffer submission outcome unknown; check the Buffer queue before retrying",
                               "submission_unknown") from e
        except httpx.TransportError as e:
            raise PublishError("Buffer submission outcome unknown; check the Buffer queue before retrying",
                               "submission_unknown") from e
        except PublishError as e:
            if e.code in ("not_connected", "http_error"):
                save(buffer_submission_pending=False)
            raise
        outcome = data.get("createPost") or {}
        post = outcome.get("post")
        if not post or not post.get("id"):
            # A typed mutation error means no post was created. Keep malformed success ambiguous.
            if outcome.get("__typename") and outcome["__typename"] != "PostActionSuccess":
                save(buffer_submission_pending=False)
                raise PublishError("Buffer refused the post; check queue limits, channel permissions and media format",
                                   "post_rejected")
            raise PublishError("Buffer returned no post ID; inspect its queue before retrying", "submission_unknown")
        save(buffer_post_id=post["id"], buffer_submission_pending=False)
        result = self._result(post)
        if result.status == "published":
            result.platform_state = req.visibility
        return result

    def cancel(self, account: dict[str, Any], external_id: str) -> None:
        data = self._graphql(account, """mutation EzraCancel($input: DeletePostInput!) {
            deletePost(input: $input) { __typename ... on MutationError { message } }
        }""", {"input": {"id": external_id}})
        if data.get("deletePost", {}).get("__typename") != "DeletePostSuccess":
            raise PublishError("Buffer could not cancel the post; inspect its queue", "cancel_failed")

    def reschedule(self, account: dict[str, Any], external_id: str, when: datetime) -> None:
        data = self._graphql(account, """mutation EzraReschedule($input: EditPostInput!) {
            editPost(input: $input) {
                ... on PostActionSuccess { post { id } } ... on MutationError { message }
            }
        }""", {"input": {"id": external_id, "mode": "customScheduled", "dueAt": when.astimezone(UTC).isoformat()}})
        if not data.get("editPost", {}).get("post", {}).get("id"):
            raise PublishError("Buffer could not reschedule the post", "reschedule_failed")

    def metrics(self, account: dict[str, Any], external_id: str) -> MetricsResult | None:
        data = self._graphql(account, """query EzraMetrics($input: PostInput!) {
            post(input: $input) { metrics { type value } }
        }""", {"input": {"id": external_id}})
        entries = data["post"].get("metrics")
        if entries is None:
            return None
        values = {m["type"]: m["value"] for m in entries}

        def count(name: str) -> int | None:
            return int(values[name]) if values.get(name) is not None else None

        return MetricsResult(views=count("views"), likes=count("likes") if "likes" in values else count("reactions"),
                             comments=count("comments"), shares=count("shares"), saves=count("saves"),
                             impressions=count("impressions"), avg_watch_seconds=values.get("averageTimeWatched"),
                             followers_gained=count("follows"), raw=values)
