"""EXTRACT step: call the YouTube Data API and keep every response page as-is.

Call plan per run (all `list` calls = 1 quota unit each):
  1. channels.list          -> channel name + hidden "uploads" playlist ID
  2. playlistItems.list     -> walk the uploads playlist newest-first until
                               videos are older than the lookback window
  3. videos.list (50 IDs)   -> title, publish time, duration, views/likes/comments
  4. playlists.list         -> the channel's public playlists        (optional)
  5. playlistItems.list     -> which of our videos sit in which playlist (optional)

A typical channel costs ~5-60 units/day, far below the 10,000 default quota.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .youtube_client import NotFoundError, QuotaExceededError, YouTubeAPIError, YouTubeClient

log = logging.getLogger(__name__)

BATCH = 50  # max IDs / maxResults the API accepts


@dataclass
class RawResponse:
    endpoint: str
    request_params: dict[str, Any]
    payload: dict[str, Any]
    fetched_at: datetime
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractResult:
    responses: list[RawResponse] = field(default_factory=list)
    channel_ids: list[str] = field(default_factory=list)
    video_ids: list[str] = field(default_factory=list)
    missing_channels: list[str] = field(default_factory=list)
    failed_channels: dict[str, str] = field(default_factory=dict)   # channel_id -> error
    truncated_channels: list[str] = field(default_factory=list)     # hit MAX_UPLOAD_PAGES
    warnings: list[str] = field(default_factory=list)

    @property
    def is_partial(self) -> bool:
        return bool(self.missing_channels or self.failed_channels or self.truncated_channels)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def chunked(items: list[str], size: int = BATCH) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class Extractor:
    def __init__(
        self,
        client: YouTubeClient,
        lookback_days: int = 28,
        fetch_playlists: bool = True,
        max_playlists_per_channel: int = 50,
        max_pages_per_playlist: int = 10,
        max_upload_pages: int = 100,
        now: datetime | None = None,
    ) -> None:
        self.client = client
        self.fetch_playlists = fetch_playlists
        self.max_playlists = max_playlists_per_channel
        self.max_pages_per_playlist = max_pages_per_playlist
        self.max_upload_pages = max_upload_pages
        self.now = now or datetime.now(timezone.utc)
        self.cutoff = self.now - timedelta(days=lookback_days)
        self.result = ExtractResult()

    # ------------------------------------------------------------------
    def run(self, channel_inputs: list[str]) -> ExtractResult:
        channels = self._resolve_channels(channel_inputs)
        for ch in channels:
            ch_id = ch["id"]
            title = ch.get("snippet", {}).get("title", ch_id)
            # Isolate failures per channel: one broken channel must not lose
            # the data of all the others. Quota errors still stop everything.
            checkpoint = len(self.result.responses)
            try:
                video_ids = self._extract_channel(ch, ch_id, title)
            except QuotaExceededError:
                raise
            except YouTubeAPIError as exc:
                # drop this channel's partial responses so it is all-or-nothing
                del self.result.responses[checkpoint:]
                self.result.failed_channels[ch_id] = str(exc)
                log.error("Channel %s (%s) failed and was skipped: %s", title, ch_id, exc)
                continue
            self.result.channel_ids.append(ch_id)
            self.result.video_ids.extend(video_ids)
        return self.result

    def _extract_channel(self, ch: dict[str, Any], ch_id: str, title: str) -> list[str]:
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads:
            log.warning("Channel %s (%s) has no uploads playlist", title, ch_id)
            return []
        video_ids = self._recent_upload_ids(uploads, ch_id)
        log.info("%s: %d video(s) published since %s", title, len(video_ids), self.cutoff.isoformat())
        self._fetch_videos(video_ids)
        if self.fetch_playlists and video_ids:
            try:
                self._fetch_playlist_membership(ch_id)
            except QuotaExceededError:
                raise
            except YouTubeAPIError as exc:
                msg = f"Playlists for channel {ch_id} skipped: {exc}"
                self.result.warnings.append(msg)
                log.warning(msg)
        return video_ids

    # ------------------------------------------------------------------
    def _record(self, endpoint: str, params: dict[str, Any], payload: dict[str, Any], **context: Any) -> None:
        self.result.responses.append(
            RawResponse(endpoint, dict(params), payload, datetime.now(timezone.utc), context)
        )

    def _resolve_channels(self, inputs: list[str]) -> list[dict[str, Any]]:
        """Accept channel IDs (UC...) and @handles."""
        ids = [c for c in inputs if not c.startswith("@")]
        handles = [c for c in inputs if c.startswith("@")]
        found: list[dict[str, Any]] = []

        for batch in chunked(ids):
            params = {"part": "snippet,contentDetails", "id": ",".join(batch), "maxResults": BATCH}
            payload = self.client.get("channels", params)
            self._record("channels", params, payload)
            items = payload.get("items", [])
            found.extend(items)
            returned = {i["id"] for i in items}
            self.result.missing_channels.extend(c for c in batch if c not in returned)

        for handle in handles:
            params = {"part": "snippet,contentDetails", "forHandle": handle}
            payload = self.client.get("channels", params)
            self._record("channels", params, payload)
            items = payload.get("items", [])
            if items:
                found.extend(items)
            else:
                self.result.missing_channels.append(handle)

        for missing in self.result.missing_channels:
            log.warning("Channel not found or not public: %s", missing)

        # de-duplicate (an ID and a handle may point to the same channel)
        unique: dict[str, dict[str, Any]] = {}
        for item in found:
            unique.setdefault(item["id"], item)
        return list(unique.values())

    def _recent_upload_ids(self, uploads_playlist_id: str, channel_id: str) -> list[str]:
        """Walk the uploads playlist (newest first) and stop once a full page
        is older than the cutoff."""
        params = {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": BATCH}
        ids: list[str] = []
        reached_cutoff = False
        try:
            for page in self.client.paginate("playlistItems", params, max_pages=self.max_upload_pages):
                self._record("playlistItems", params, page, kind="uploads",
                             playlist_id=uploads_playlist_id, channel_id=channel_id)
                items = page.get("items", [])
                in_window_on_page = 0
                for item in items:
                    cd = item.get("contentDetails", {})
                    published = parse_ts(cd.get("videoPublishedAt"))
                    if published is None:          # private / deleted video
                        continue
                    if published < self.cutoff:
                        reached_cutoff = True
                    else:
                        in_window_on_page += 1
                        if cd.get("videoId") and cd["videoId"] not in ids:
                            ids.append(cd["videoId"])
                if items and in_window_on_page == 0:
                    break
            else:
                # The loop ran out of pages without reaching videos older than
                # the cutoff. If that is because of our page cap (not the end
                # of the playlist), older in-window videos were NOT collected.
                if page.get("nextPageToken") and not reached_cutoff:
                    msg = (f"Channel {channel_id}: stopped after {self.max_upload_pages} pages "
                           f"({self.max_upload_pages * BATCH} uploads) but videos were still inside the "
                           f"{(self.now - self.cutoff).days}-day window - raise MAX_UPLOAD_PAGES")
                    self.result.truncated_channels.append(channel_id)
                    self.result.warnings.append(msg)
                    log.warning(msg)
        except NotFoundError:
            # YouTube returns 404 for the uploads playlist of a channel that
            # has never uploaded. That is "no videos", not a failure.
            log.info("Channel %s has no public uploads (uploads playlist not found)", channel_id)
        return ids

    def _fetch_videos(self, video_ids: list[str]) -> None:
        for batch in chunked(video_ids):
            params = {"part": "snippet,contentDetails,statistics", "id": ",".join(batch), "maxResults": BATCH}
            payload = self.client.get("videos", params)
            self._record("videos", params, payload)

    def _fetch_playlist_membership(self, channel_id: str) -> None:
        params = {"part": "snippet,contentDetails", "channelId": channel_id, "maxResults": BATCH}
        max_pages = max(1, -(-self.max_playlists // BATCH))  # ceil division
        playlists: list[dict[str, Any]] = []
        for page in self.client.paginate("playlists", params, max_pages=max_pages):
            self._record("playlists", params, page, channel_id=channel_id)
            playlists.extend(page.get("items", []))
        playlists = playlists[: self.max_playlists]

        for pl in playlists:
            if int(pl.get("contentDetails", {}).get("itemCount", 0) or 0) == 0:
                continue
            item_params = {"part": "snippet,contentDetails", "playlistId": pl["id"], "maxResults": BATCH}
            try:
                for page in self.client.paginate("playlistItems", item_params, max_pages=self.max_pages_per_playlist):
                    self._record("playlistItems", item_params, page, kind="playlist",
                                 playlist_id=pl["id"], channel_id=channel_id)
            except QuotaExceededError:
                raise
            except YouTubeAPIError as exc:
                # Playlist info is optional ("can be empty"), so a single
                # broken/deleted playlist is a warning, not a failed channel.
                msg = f"Playlist {pl['id']} skipped: {exc}"
                self.result.warnings.append(msg)
                log.warning(msg)
