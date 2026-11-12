"""Shared pieces for every platform monitor.

A platform monitor subclasses `PlatformMonitor`, sets `platform = "<name>"` and
implements `_check()`. Subclasses register themselves automatically, so
`StreamMonitor` can build the right monitor from a config entry's `platform` field.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, ClassVar

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class LiveState(StrEnum):
    LIVE = "live"
    UPCOMING = "upcoming"  # scheduled / waiting room open
    OFFLINE = "offline"
    UNKNOWN = "unknown"  # check failed


@dataclass(frozen=True)
class Streamer:
    name: str
    platform: str
    channel: str  # platform-specific id: YouTube handle "@foo" or channel id "UC..."

    @property
    def id(self) -> str:
        """Stable key (display name may change in config)."""
        return f"{self.platform}:{self.channel}"


@dataclass
class StreamStatus:
    streamer: Streamer
    state: LiveState
    title: str | None = None
    url: str | None = None
    # Identifies one broadcast (YouTube video id, Twitch start time). Lets consumers
    # tell "same stream" from "a new stream". None when the platform didn't say.
    stream_id: str | None = None
    started_at: datetime | None = None
    viewers: int | None = None
    thumbnail: str | None = None  # stream cover image (live / upcoming)
    avatar: str | None = None  # channel avatar
    error: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_live(self) -> bool:
        return self.state is LiveState.LIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.streamer.name,
            "platform": self.streamer.platform,
            "channel": self.streamer.channel,
            "state": self.state.value,
            "title": self.title,
            "url": self.url,
            "stream_id": self.stream_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "viewers": self.viewers,
            "thumbnail": self.thumbnail,
            "avatar": self.avatar,
            "error": self.error,
            "checked_at": self.checked_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StreamStatus:
        return cls(
            streamer=Streamer(d["name"], d["platform"], d["channel"]),
            state=LiveState(d["state"]),
            title=d.get("title"),
            url=d.get("url"),
            stream_id=d.get("stream_id"),
            started_at=parse_iso(d.get("started_at")),
            viewers=d.get("viewers"),
            thumbnail=d.get("thumbnail"),
            avatar=d.get("avatar"),
            error=d.get("error"),
            checked_at=parse_iso(d.get("checked_at")) or datetime.now(timezone.utc),
        )

    def same_stream(self, other: StreamStatus) -> bool:
        """Both live and the same broadcast. Unknown ids count as the same, so a
        missing field never looks like a brand-new stream."""
        if not (self.is_live and other.is_live):
            return False
        return self.stream_id is None or other.stream_id is None or self.stream_id == other.stream_id


class PlatformMonitor(ABC):
    """Base class for one streaming platform."""

    platform: ClassVar[str]
    registry: ClassVar[dict[str, type[PlatformMonitor]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "platform", None):
            PlatformMonitor.registry[cls.platform] = cls

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def check(self, streamer: Streamer) -> StreamStatus:
        """Check one streamer. Never raises: failures come back as UNKNOWN."""
        try:
            return await self._check(streamer)
        except Exception as e:  # noqa: BLE001 - one bad channel must not stop the loop
            msg = str(e).splitlines()[0] if str(e) else ""
            log.debug("%s check failed for %s: %s", self.platform, streamer.name, e)
            return StreamStatus(streamer, LiveState.UNKNOWN, error=f"{type(e).__name__}: {msg}")

    @abstractmethod
    async def _check(self, streamer: Streamer) -> StreamStatus:
        """Platform-specific check. May raise; `check()` handles it."""

    # ---- helpers for subclasses ----

    async def fetch_text(self, url: str, **kwargs: Any) -> str:
        resp = await self.client.get(url, follow_redirects=True, **kwargs)
        resp.raise_for_status()
        return resp.text


def extract_json_after(text: str, marker: str) -> dict[str, Any] | None:
    """Parse the JSON object that follows `marker` in an HTML page, e.g.
    `var ytInitialPlayerResponse = {...};`. Returns None if marker is absent."""
    i = text.find(marker)
    if i < 0:
        return None
    start = text.find("{", i + len(marker))
    if start < 0:
        return None
    obj, _ = json.JSONDecoder().raw_decode(text, start)
    return obj


def meta_content(page: str, prop: str) -> str | None:
    """Value of `<meta property="{prop}" content="...">` (e.g. og:image), unescaped."""
    m = re.search(rf'<meta (?:property|name)="{re.escape(prop)}" content="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else None


def ld_json_nodes(page: str) -> list[dict[str, Any]]:
    """All schema.org JSON-LD objects in a page, with `@graph` lists flattened."""
    nodes: list[dict[str, Any]] = []
    for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                nodes.extend(item.get("@graph") or [item])
    return [n for n in nodes if isinstance(n, dict)]


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )


OnChange = Callable[[StreamStatus, StreamStatus | None], Awaitable[None] | None]


class StreamMonitor:
    """Polls a list of streamers across platforms and reports state changes."""

    def __init__(self, streamers: Iterable[Streamer], concurrency: int = 8):
        self.streamers = list(streamers)
        self.client = make_client()
        self._sem = asyncio.Semaphore(concurrency)
        self._monitors: dict[str, PlatformMonitor] = {}
        self.statuses: dict[Streamer, StreamStatus] = {}
        self.last_refresh: datetime | None = None
        self.on_change: list[OnChange] = []  # called as cb(new, old) on state change
        self._refresh_lock = asyncio.Lock()

        for s in self.streamers:
            if s.platform not in PlatformMonitor.registry:
                known = ", ".join(sorted(PlatformMonitor.registry)) or "none"
                raise ValueError(f"{s.name}: unknown platform {s.platform!r} (known: {known})")

    def set_streamers(self, streamers: Iterable[Streamer]) -> None:
        """Replace the list while running; removed streamers lose their status."""
        self.streamers = list(streamers)
        for s in list(self.statuses):
            if s not in self.streamers:
                del self.statuses[s]

    def monitor_for(self, platform: str) -> PlatformMonitor:
        if platform not in self._monitors:
            self._monitors[platform] = PlatformMonitor.registry[platform](self.client)
        return self._monitors[platform]

    async def _check(self, s: Streamer) -> StreamStatus:
        async with self._sem:
            return await self.monitor_for(s.platform).check(s)

    async def poll_once(self) -> list[StreamStatus]:
        results = await asyncio.gather(*(self._check(s) for s in self.streamers))
        return list(results)

    async def refresh(self) -> list[tuple[StreamStatus, StreamStatus | None]]:
        """Poll everyone, update `self.statuses`, call every `on_change(new, old)`
        callback for each state change (including the first check), and return the
        changes. UNKNOWN results don't overwrite a known state, so a single network
        blip doesn't fire a false "went offline"."""
        async with self._refresh_lock:
            changes = []
            for status in await self.poll_once():
                old = self.statuses.get(status.streamer)
                if status.state is LiveState.UNKNOWN and old and old.state is not LiveState.UNKNOWN:
                    continue
                self.statuses[status.streamer] = status
                if (
                    old is None
                    or old.state != status.state
                    # Ended and restarted between two polls: live -> live, new broadcast.
                    or (old.is_live and not status.same_stream(old))
                ):
                    changes.append((status, old))
            self.last_refresh = datetime.now(timezone.utc)
            for new, old in changes:
                for cb in self.on_change:
                    try:
                        result = cb(new, old)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        # A broken callback must never stop monitoring.
                        log.exception("on_change callback failed for %s", new.streamer.name)
            return changes

    async def run(self, interval: float) -> None:
        """Poll forever; never dies on an error. Register `self.on_change` first."""
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("refresh failed; retrying next interval")
            await asyncio.sleep(interval)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> StreamMonitor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
