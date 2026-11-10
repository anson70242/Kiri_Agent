"""Shared test fixtures: sample streamers/statuses and a recording notifier."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from backend.monitor import LiveState, Streamer, StreamStatus
from backend.notify import EventKind, Notifier, StreamEvent

T0 = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)
A = Streamer("A", "youtube", "@a")
B = Streamer("B", "twitch", "b")


def live(s: Streamer = A, sid: str | None = "v1", started: datetime = T0, **kw) -> StreamStatus:
    return StreamStatus(s, LiveState.LIVE, title=f"title {sid}", url=f"https://x/{sid}",
                        stream_id=sid, started_at=started, **kw)


def off(s: Streamer = A) -> StreamStatus:
    return StreamStatus(s, LiveState.OFFLINE)


def up(s: Streamer = A) -> StreamStatus:
    return StreamStatus(s, LiveState.UPCOMING)


def unk(s: Streamer = A) -> StreamStatus:
    return StreamStatus(s, LiveState.UNKNOWN, error="boom")


class Recorder(Notifier):
    """Records delivered events; can fail the first `fail` sends or be slow."""

    name = ""  # empty: not added to the registry

    def __init__(self, fail: int = 0, exc: type[Exception] = RuntimeError, delay: float = 0):
        self.events: list[tuple[EventKind, str, str | None]] = []
        self.raw: list[StreamEvent] = []
        self.calls = 0
        self.fail = fail
        self.exc = exc
        self.delay = delay

    async def send(self, event: StreamEvent) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail > 0:
            self.fail -= 1
            raise self.exc("simulated failure")
        self.events.append((event.kind, event.status.streamer.id, event.status.stream_id))
        self.raw.append(event)


LIVE, OFFLINE = EventKind.LIVE, EventKind.OFFLINE


def ev(kind: EventKind, sid: str | None = "v1", s: Streamer = A) -> tuple[EventKind, str, str | None]:
    return (kind, s.id, sid)


def later(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)
