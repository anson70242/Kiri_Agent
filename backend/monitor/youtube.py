"""YouTube live check, no API key needed.

Fetches `youtube.com/<channel>/live`. When the channel is live (or has a scheduled
stream), YouTube serves that stream's watch page, whose embedded
`ytInitialPlayerResponse` says whether it is live. Otherwise it serves the channel page.

`/live` can serve a scheduled item even while another stream is live: seen with a
members-only stream running next to a far-future premiere. So whenever `/live` doesn't
say LIVE, the channel's `/streams` tab is checked for an item with a LIVE badge.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from typing import Any

from .base import (
    LiveState,
    PlatformMonitor,
    Streamer,
    StreamStatus,
    extract_json_after,
    meta_content,
    parse_iso,
)

# Skips the EU cookie-consent interstitial.
CONSENT_HEADERS = {"Cookie": "SOCS=CAI"}

CANONICAL_RE = re.compile(r'<link rel="canonical" href="([^"]+)"')
OWNER_AVATAR_RE = re.compile(r'"videoOwnerRenderer":\{"thumbnail":\{"thumbnails":\[\{"url":"([^"]+)"')


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return html.unescape(m.group(1)) if m else None


class YouTubeMonitor(PlatformMonitor):
    platform = "youtube"

    @staticmethod
    def live_url(channel: str) -> str:
        channel = channel.strip()
        if channel.startswith("http"):
            return channel.rstrip("/") + ("" if channel.rstrip("/").endswith("/live") else "/live")
        if channel.startswith("UC"):
            return f"https://www.youtube.com/channel/{channel}/live"
        handle = channel if channel.startswith("@") else f"@{channel}"
        return f"https://www.youtube.com/{handle}/live"

    @classmethod
    def streams_url(cls, channel: str) -> str:
        return cls.live_url(channel).removesuffix("/live") + "/streams"

    async def _check(self, streamer: Streamer) -> StreamStatus:
        page = await self.fetch_text(self.live_url(streamer.channel), headers=CONSENT_HEADERS)
        status = self.parse_page(streamer, page)
        if status.state is LiveState.LIVE:
            return status
        # A failure here raises (-> UNKNOWN): reporting /live's answer instead could
        # end a members-only stream that only /streams shows.
        streams = await self.fetch_text(self.streams_url(streamer.channel), headers=CONSENT_HEADERS)
        live = self.parse_streams_page(streamer, streams)
        if live is None:
            return status
        live.avatar = status.avatar
        return live

    @classmethod
    def parse_streams_page(cls, streamer: Streamer, page: str) -> StreamStatus | None:
        """The LIVE item on a channel's /streams tab (members-only included), or None.
        Raises on a page without `ytInitialData` (consent wall, bot check)."""
        data = extract_json_after(page, "var ytInitialData =")
        if data is None:
            raise ValueError("unrecognized YouTube streams page (consent / bot check?)")
        for kind, item in _video_items(data):
            if kind == "lockupViewModel":
                video_id = item.get("contentId")
                title = (((item.get("metadata") or {}).get("lockupMetadataViewModel") or {}).get("title") or {}).get("content")
                thumb = (item.get("contentImage") or {}).get("thumbnailViewModel") or {}
                sources = (thumb.get("image") or {}).get("sources") or []
                is_live = any(
                    b.get("badgeStyle") == "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE"
                    for b in _find_key(thumb.get("overlays"), "thumbnailBadgeViewModel")
                )
            else:  # older videoRenderer layout
                video_id = item.get("videoId")
                runs = (item.get("title") or {}).get("runs") or [{}]
                title = "".join(r.get("text", "") for r in runs) or None
                sources = (item.get("thumbnail") or {}).get("thumbnails") or []
                is_live = any(
                    o.get("style") == "LIVE" for o in _find_key(item.get("thumbnailOverlays"), "thumbnailOverlayTimeStatusRenderer")
                ) or any(
                    b.get("style") == "BADGE_STYLE_TYPE_LIVE_NOW" for b in _find_key(item.get("badges"), "metadataBadgeRenderer")
                )
            if is_live and video_id:
                return StreamStatus(
                    streamer,
                    LiveState.LIVE,
                    title=title,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    stream_id=video_id,
                    thumbnail=max(sources, key=lambda s: s.get("width") or 0).get("url") if sources else None,
                )
        return None

    @classmethod
    def parse_page(cls, streamer: Streamer, page: str) -> StreamStatus:
        """Raises on a page we don't recognise (consent wall, bot check, error page)
        rather than guessing "offline", which would fire a false "stream ended"."""
        canonical = _first(CANONICAL_RE, page) or ""
        player = extract_json_after(page, "var ytInitialPlayerResponse =")
        og_image = meta_content(page, "og:image")
        if player and player.get("videoDetails"):
            # Watch page: live / upcoming, or a just-ended stream (offline).
            status = cls.parse_player(streamer, player)
            if status.state is not LiveState.OFFLINE:
                status.thumbnail = og_image  # the stream cover
            avatar = _first(OWNER_AVATAR_RE, page)
        elif "/channel/" in canonical:
            status = StreamStatus(streamer, LiveState.OFFLINE)  # channel page: offline
            avatar = og_image  # og:image is the channel avatar
        else:
            raise ValueError("unrecognized YouTube page (consent / bot check?)")
        status.avatar = re.sub(r"=s\d+-", "=s176-", avatar) if avatar else None
        return status

    @staticmethod
    def parse_player(streamer: Streamer, player: dict[str, Any] | None) -> StreamStatus:
        # No player response means YouTube served the channel page: offline.
        player = player or {}
        details = player.get("videoDetails") or {}
        micro = (player.get("microformat") or {}).get("playerMicroformatRenderer") or {}
        broadcast = micro.get("liveBroadcastDetails") or {}

        if details.get("isLive") or broadcast.get("isLiveNow"):
            state = LiveState.LIVE
        elif details.get("isUpcoming"):
            state = LiveState.UPCOMING
        else:
            # Channel page, or a just-finished stream still being served: offline.
            return StreamStatus(streamer, LiveState.OFFLINE)

        video_id = details.get("videoId")
        return StreamStatus(
            streamer,
            state,
            title=details.get("title"),
            url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            stream_id=video_id,
            started_at=parse_iso(broadcast.get("startTimestamp")),
            # Concurrent viewer count isn't in the page; needs the Data API.
        )


def _find_key(obj: Any, key: str) -> Iterator[dict[str, Any]]:
    """Every dict value stored under `key`, anywhere inside `obj`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, dict):
                yield v
            else:
                yield from _find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_key(v, key)


def _video_items(data: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Video items on a channel tab, in page order, in either the new or old layout."""
    def walk(obj: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("lockupViewModel", "videoRenderer", "gridVideoRenderer") and isinstance(v, dict):
                    yield ("lockupViewModel" if k == "lockupViewModel" else "videoRenderer"), v
                else:
                    yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)

    return walk(data)
