"""TwitCasting live check, no API key needed.

Fetches the public channel page `twitcasting.tv/<channel id>` (one request a check).
Markers verified on the real site (2026-11-13): the body's `data-is-onlive` says
live or not, `data-movie-id` is the live's id (a Live is one TwitCasting movie),
`data-started-at` is its start time in milliseconds (real only while live),
`og:title` carries the title, `og:image` the cover and
`data-broadcaster-profile-image` the avatar. An unknown channel id answers HTTP 200
with the site's front page, which has no `data-is-onlive`: that is an error, never
offline. TwitCasting has no scheduled "waiting room", so states are only live /
offline.

`channel_id()` and `live_title()` are shared with the TwitCasting downloader.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from .base import LiveState, PlatformMonitor, Streamer, StreamStatus, meta_content

SITE = "https://twitcasting.tv"


def channel_id(channel: str) -> str:
    """Accept `name`, `@name` or a twitcasting.tv address (a channel, a replay or a
    comment page); return the channel id. Ids may contain a colon (`c:name`, `g:123`)."""
    channel = channel.strip()
    if "twitcasting.tv/" in channel:
        channel = channel.split("twitcasting.tv/", 1)[1]
    channel = channel.split("?", 1)[0].split("#", 1)[0].strip("/").split("/", 1)[0]
    return channel.lstrip("@")


def channel_url(channel: str) -> str:
    """The channel's address: the watch address of every live of the channel."""
    return f"{SITE}/{channel_id(channel)}"


def live_title(og_title: str) -> str:
    """A live's own title from a page's `og:title`. TwitCasting appends the channel
    title as `<live title> / <channel title>`, or shows only the channel title when the
    live has none: drop the last ` / ` and what follows; keep a title without one whole."""
    head, sep, _ = og_title.rpartition(" / ")
    return head if sep else og_title


def data_attr(page: str, name: str) -> str | None:
    """Value of the first `data-<name>="..."` attribute in a page, unescaped."""
    m = re.search(rf'\bdata-{re.escape(name)}="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else None


class TwitCastingMonitor(PlatformMonitor):
    platform = "twitcasting"

    async def _check(self, streamer: Streamer) -> StreamStatus:
        url = channel_url(streamer.channel)
        page = await self.fetch_text(url)
        return self.parse_page(streamer, url, page)

    @staticmethod
    def parse_page(streamer: Streamer, url: str, page: str) -> StreamStatus:
        onlive = data_attr(page, "is-onlive")
        if onlive not in ("true", "false"):
            raise ValueError(
                f"no live marker on {url} (wrong channel id, or the TwitCasting page changed)"
            )
        avatar = data_attr(page, "broadcaster-profile-image") or None
        if onlive == "false":
            return StreamStatus(streamer, LiveState.OFFLINE, url=url, avatar=avatar)

        started_ms = data_attr(page, "started-at")
        started = (
            datetime.fromtimestamp(int(started_ms) / 1000, timezone.utc)
            if started_ms and started_ms.isdigit()
            else None
        )
        og_title = meta_content(page, "og:title")
        return StreamStatus(
            streamer,
            LiveState.LIVE,
            title=live_title(og_title) if og_title else None,
            url=url,
            stream_id=data_attr(page, "movie-id") or None,
            started_at=started,
            thumbnail=meta_content(page, "og:image") or None,
            avatar=avatar,
        )
