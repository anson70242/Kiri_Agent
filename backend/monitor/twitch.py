"""Twitch live check, no API key needed.

Fetches the public channel page `twitch.tv/<login>`. Its schema.org JSON-LD holds a
`VideoObject` with `publication.isLiveBroadcast: true` while the channel is live, plus
the stream title, preview image and start time. The `ProfilePage` node has the avatar.
Twitch has no scheduled "waiting room", so states are only live / offline.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .base import (
    LiveState,
    PlatformMonitor,
    Streamer,
    StreamStatus,
    ld_json_nodes,
    meta_content,
    parse_iso,
)

VIEWERS_RE = re.compile(r"for ([\d,]+) viewers")


class TwitchMonitor(PlatformMonitor):
    platform = "twitch"

    @staticmethod
    def login(channel: str) -> str:
        """Accept `name`, `@name` or a twitch.tv URL; return the lowercase login."""
        channel = channel.strip().rstrip("/")
        if "twitch.tv/" in channel:
            channel = channel.split("twitch.tv/", 1)[1].split("/")[0].split("?")[0]
        return channel.lstrip("@").lower()

    async def _check(self, streamer: Streamer) -> StreamStatus:
        login = self.login(streamer.channel)
        url = f"https://www.twitch.tv/{login}"
        # Unknown logins get a generic page without the profile node, and Twitch also
        # occasionally serves that generic page for real channels, so retry once.
        for attempt in range(2):
            page = await self.fetch_text(url, headers={"Accept-Language": "en-US,en;q=0.9"})
            nodes = ld_json_nodes(page)
            if any(n.get("@type") == "ProfilePage" for n in nodes):
                break
            if attempt == 0:
                await asyncio.sleep(1)
        else:
            raise ValueError(f"no channel data for {login!r} (wrong name, or Twitch hiccup)")
        return self.parse_page(streamer, url, nodes, meta_content(page, "og:description"))

    @staticmethod
    def parse_page(
        streamer: Streamer, url: str, nodes: list[dict[str, Any]], og_description: str | None
    ) -> StreamStatus:
        profile = next(n for n in nodes if n.get("@type") == "ProfilePage")
        avatar = (profile.get("mainEntity") or {}).get("image")

        video = next(
            (
                n
                for n in nodes
                if n.get("@type") == "VideoObject"
                and (n.get("publication") or {}).get("isLiveBroadcast")
            ),
            None,
        )
        if not video:
            return StreamStatus(streamer, LiveState.OFFLINE, url=url, avatar=avatar)

        thumbs = video.get("thumbnailUrl") or []
        thumb = thumbs[-1] if isinstance(thumbs, list) and thumbs else thumbs or None
        start_raw = (video.get("publication") or {}).get("startDate")
        started = parse_iso(start_raw)
        if thumb and started:
            # Preview URL never changes; bust browser cache so it shows a fresh frame.
            thumb = f"{thumb}?t={int(started.timestamp())}"

        viewers = None
        if og_description and (m := VIEWERS_RE.search(og_description)):
            viewers = int(m.group(1).replace(",", ""))

        return StreamStatus(
            streamer,
            LiveState.LIVE,
            title=video.get("description"),
            url=url,
            # Twitch channel URL is the same for every stream; the start time isn't.
            stream_id=start_raw or None,
            started_at=started,
            viewers=viewers,
            thumbnail=thumb,
            avatar=avatar,
        )
