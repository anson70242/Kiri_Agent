"""YouTube live check, no API key needed.

Fetches `youtube.com/<channel>/live`. When the channel is live (or has a scheduled
stream), YouTube serves that stream's watch page, whose embedded
`ytInitialPlayerResponse` says whether it is live. Otherwise it serves the channel page.
"""

from __future__ import annotations

import html
import re
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

    async def _check(self, streamer: Streamer) -> StreamStatus:
        page = await self.fetch_text(self.live_url(streamer.channel), headers=CONSENT_HEADERS)
        return self.parse_page(streamer, page)

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
