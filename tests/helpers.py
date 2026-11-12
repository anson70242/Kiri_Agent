"""Shared test fixtures: sample streamers/statuses, a recording notifier, sample archives."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


# ---- sample archives ----

CHAT = [
    {"time": "00:00:05", "user": "a", "msg": "hi"},
    {"time": "00:00:30", "user": "b", "msg": "yo"},
    {"time": "00:02:10", "user": "c", "msg": "8888", "sc": "¥500"},
]
TRANSCRIPT = [{"start": 1.0, "end": 2.0, "text": "やっほー"}]
ZH_TRANSCRIPT = [{"start": 1.0, "end": 2.0, "text": "哈喽"}]
HIGHLIGHTS = {"streamer": "柊優花", "title": "歌枠", "rows": [
    {"start": "00:00:00", "end": "00:05:00", "score": 8, "top_pick": {"title": "开场"}},
    {"start": "00:05:00", "end": "00:10:00", "score": 5, "top_pick": None},
]}
_ARTIFACT_FILES = {  # keyword -> file name suffix, spelled out here as the tests' own source of truth
    "video": ".mp4", "raw_chat": ".live_chat.json", "chat": ".chat_parsed.json",
    "transcript": ".transcript.json", "zh_transcript": ".zh.transcript.json", "highlights": ".highlights.json",
}


def sample_archive(root: Path, creator: str = "柊優花", date: str = "20261105", title: str = "歌枠", *,
                   video: bytes | None = b"0123456789", raw_chat: object = None, chat: object = CHAT,
                   transcript: object = TRANSCRIPT, zh_transcript: object = ZH_TRANSCRIPT,
                   highlights: object = HIGHLIGHTS, stem: str | None = None) -> Path:
    """Build `<root>/<creator>/<date>/<title>/` with the named artifacts, each `<stem><suffix>`
    (stem `[date][creator] title` by default). An artifact given None is left out; bytes or
    str are written as they are (e.g. damaged JSON), anything else as JSON. Returns the folder."""
    folder = root / creator / date / title
    folder.mkdir(parents=True, exist_ok=True)
    stem = stem if stem is not None else f"[{date}][{creator}] {title}"
    given = {"video": video, "raw_chat": raw_chat, "chat": chat, "transcript": transcript,
             "zh_transcript": zh_transcript, "highlights": highlights}
    for key, content in given.items():
        if content is None:
            continue
        path = folder / f"{stem}{_ARTIFACT_FILES[key]}"
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return folder


# Ports Chromium refuses to open (ERR_UNSAFE_PORT), for the browser tests' servers.
CHROME_UNSAFE = {1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668,
                 6669, 6679, 6697, 10080}


def free_port() -> int:
    """A free local port that Chromium will open."""
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in CHROME_UNSAFE:
            return port
