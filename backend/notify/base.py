"""Shared pieces for every notification channel.

A channel subclasses `Notifier`, sets `name = "<channel>"` and implements `send()`.
`LiveNotifier` decides *when* to notify (went live / ended), remembers state across
restarts, and delivers events in order with retries.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from backend.monitor import LiveState, StreamStatus
from backend.state_file import StateFile

from .history import NotifyHistory

log = logging.getLogger(__name__)


class EventKind(StrEnum):
    LIVE = "live"  # went live
    OFFLINE = "offline"  # stream ended
    DOWNLOADED = "downloaded"  # archive auto-downloaded (backend.downloader.auto)
    # the replay of a live is not published; it is downloaded if it appears in time
    REPLAY_UNPUBLISHED = "replay_unpublished"
    DOWNLOAD_FAILED = "download_failed"  # auto-download gave up
    # deleting a creator's old archives after a download failed; the new archive is fine
    PRUNE_FAILED = "prune_failed"
    TRANSCRIBE_FAILED = "transcribe_failed"  # speech-to-text of a downloaded archive gave up
    ANALYZED = "analyzed"  # 路灯表 ready (detail: the top segments)
    ANALYZE_FAILED = "analyze_failed"  # 路灯表 analysis gave up
    TRANSLATE_FAILED = "translate_failed"  # Chinese subtitles gave up
    UPLOADED = "uploaded"  # 路灯表 uploaded to Tencent Docs (detail: the sheet link)
    UPLOAD_FAILED = "upload_failed"  # Tencent Docs upload gave up
    READY = "ready"  # every stage of a stream is over (detail: what's done, the download links)


@dataclass
class StreamEvent:
    kind: EventKind
    status: StreamStatus  # for OFFLINE / DOWNLOAD*: the last live status (title, url, start time)
    ended_at: datetime | None = None
    detail: str | None = None  # DOWNLOAD*: what happened, shown in the message


class PermanentNotifyError(Exception):
    """Delivery can never succeed (bad webhook URL, rejected payload): don't retry."""


class Notifier(ABC):
    """Base class for one notification channel (Discord, ...)."""

    name: ClassVar[str]
    registry: ClassVar[dict[str, type[Notifier]]] = {}
    logs_itself: ClassVar[bool] = False  # records its own deliveries in the history (QQ: per group)

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "name", None):
            Notifier.registry[cls.name] = cls

    @abstractmethod
    async def send(self, event: StreamEvent) -> None:
        """Deliver one event. Raise PermanentNotifyError for unrecoverable failures;
        anything else is retried."""

    async def aclose(self) -> None:
        pass


class LiveNotifier:
    """Turns monitor state changes into go-live / ended events.

    Register `on_change` in `StreamMonitor.on_change`.

    Rules, per streamer. A sitting is one or more lives in a row that the group sees as
    one: it gets one "went live" and one "ended".
    - Went live: a new sitting is seen live. Never announced twice. On a fresh start
      (no saved state at all) streamers already live are skipped unless
      `notify_on_startup`; a streamer newly added to an existing setup is announced.
    - Ended: the live stays not-live for `offline_grace` seconds. If the same live comes
      back within that window nothing is sent. If a *different* live appears:
      - at the same watch address, straight after the previous live or inside its
        grace window, it continues the sitting: nothing is sent;
      - otherwise (another address, a live saved before a restart) the sitting is
        reported ended first, then the new one live.
    - Every LIVE is eventually followed by exactly one OFFLINE, which carries the id and
      start time of the live that was announced and the end time of the sitting's last
      live.
    - `on_ended` listeners get every live once, with that live's own details, also the
      lives that a sitting continued past.

    With `state_file`, the last live per streamer survives restarts, so a live that
    ended (or a new one that started) while the monitor was down, or during a pending
    grace window, is still reported after restart. A sitting that continued past its
    first live saves that live too, so its "ended" after a restart still carries it.

    Events go through one queue: delivered in order, retried with backoff, and never
    block polling.
    """

    def __init__(
        self,
        notifiers: Iterable[Notifier],
        notify_on_startup: bool = False,
        notify_offline: bool = True,
        offline_grace: float = 120,
        state_file: Path | None = None,
        max_attempts: int = 5,
        retry_base_delay: float = 2.0,
        history: NotifyHistory | None = None,
    ):
        self.notifiers = list(notifiers)
        self.history = history or NotifyHistory()  # what was sent, for the 通知 page
        self.qq = next((n for n in self.notifiers if n.name == "qq"), None)
        self.notify_on_startup = notify_on_startup
        self.notify_offline = notify_offline
        self.offline_grace = offline_grace
        self.state_file = state_file
        # Runtime state: a damaged file is set aside as a backup (the owner is alerted)
        # and counts as no saved state at all.
        self._state = StateFile(state_file, default=SavedLives, policy="recover", decode=decode_live_state,
                                encode=encode_live_state) if state_file else None
        self.max_attempts = max_attempts
        self.retry_base_delay = retry_base_delay
        # streamer id -> current live broadcast, or None if known not live.
        # A missing key means "never seen" (first sighting).
        saved = self._state.load() if self._state else SavedLives()
        self._live: dict[str, StreamStatus | None] = saved.live
        # No saved state at all (first deploy, wiped volume): whoever is live now was
        # live before we existed, so don't flood the channel. With saved state, a
        # streamer missing from it was just added to the config and is announced.
        self._fresh_start = not self._live
        # Streamers whose current live was seen live by this run: only those can continue
        # into a new live (a saved live that differs after a restart is reported ended).
        self._watched: set[str] = set()
        # streamer id -> the first live of the current sitting, whose id and start time
        # "went live" and "ended" carry. Saved with the live state, so a sitting that
        # continued past its first live keeps its start time across a restart.
        self._sitting: dict[str, StreamStatus] = saved.sittings
        self._pending_end: dict[str, asyncio.Task[None]] = {}
        # Called with every "ended" event (even when on_offline is off), e.g. to
        # start the auto-download. Must return quickly.
        self.on_ended: list[Callable[[StreamEvent], None]] = []
        self._queue: asyncio.Queue[StreamEvent] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._alerts: set[asyncio.Task[None]] = set()

    # ---- state rules ----

    async def on_change(self, new: StreamStatus, old: StreamStatus | None = None) -> None:
        if new.state is LiveState.UNKNOWN:
            return
        sid = new.streamer.id
        first_sighting = sid not in self._live
        prev = self._live.get(sid)

        if new.is_live:
            same = prev is not None and prev.same_stream(new)
            # A different live at the same watch address, while the previous one is
            # still live or inside its grace period, both seen by this run.
            continues = (prev is not None and not same and sid in self._watched
                         and same_address(prev, new))
            pending = self._pending_end.pop(sid, None)
            if pending:
                pending.cancel()
            if continues:
                self._end_live(prev, new.checked_at)
            elif prev is not None and not same:
                # The previous live ended (between polls, inside the grace
                # window, or while we were down) and a new sitting started.
                self._end_sitting(sid, prev, new.checked_at)
            if same:
                # Keep the id it was announced with, so LIVE and its OFFLINE always
                # carry the same stream_id; refresh everything else (title, cover).
                new = replace(new, stream_id=prev.stream_id)
                if self._sitting.get(sid) is prev:
                    self._sitting[sid] = new
            elif not continues:
                self._sitting[sid] = new
            self._live[sid] = new
            self._watched.add(sid)
            self._save_state()
            skip = first_sighting and self._fresh_start and not self.notify_on_startup
            if not same and not continues and not skip:
                self._enqueue(StreamEvent(EventKind.LIVE, new))
        elif prev is not None:
            if sid not in self._pending_end:
                self._pending_end[sid] = asyncio.create_task(
                    self._end_after_grace(sid, prev, new.checked_at)
                )
        elif first_sighting:
            self._live[sid] = None
            self._save_state()

    async def _end_after_grace(self, sid: str, prev: StreamStatus, ended_at: datetime) -> None:
        await asyncio.sleep(self.offline_grace)
        self._pending_end.pop(sid, None)
        if self._live.get(sid) is prev:
            self._live[sid] = None
            self._watched.discard(sid)
            self._end_sitting(sid, prev, ended_at)
            self._save_state()

    def _end_sitting(self, sid: str, last: StreamStatus, ended_at: datetime) -> None:
        """The sitting whose last live is `last` is over: the listeners get `last`, the
        "ended" notification the announced live's id and start time with `last`'s details."""
        self._end_live(last, ended_at)
        first = self._sitting.pop(sid, None)
        if first is not None and first is not last:
            last = replace(last, stream_id=first.stream_id, started_at=first.started_at)
        if self.notify_offline:
            self._enqueue(StreamEvent(EventKind.OFFLINE, last, ended_at))

    def _end_live(self, live: StreamStatus, ended_at: datetime) -> None:
        """Tell the "a live ended" listeners about one live, with its own details."""
        event = StreamEvent(EventKind.OFFLINE, live, ended_at)
        for callback in self.on_ended:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - a listener bug must not break notifications
                log.exception("on_ended listener failed")

    # ---- delivery ----

    def send(self, event: StreamEvent) -> None:
        """Queue any event (e.g. a download result) for delivery; doesn't wait."""
        self._enqueue(event)

    def _enqueue(self, event: StreamEvent) -> None:
        if not self.notifiers:
            return
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._worker = asyncio.create_task(self._run_worker())
        self._queue.put_nowait(event)

    async def _run_worker(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                await asyncio.gather(*(self._deliver(n, event) for n in self.notifiers))
            finally:
                self._queue.task_done()

    async def _deliver(self, notifier: Notifier, event: StreamEvent) -> None:
        name = event.status.streamer.name
        summary = f"{event.kind}: {name} {event.status.title or ''}".strip()
        record = not notifier.logs_itself
        for attempt in range(1, self.max_attempts + 1):
            try:
                await notifier.send(event)
                log.info("%s: notified %s %s", notifier.name, name, event.kind)
                if record:
                    self.history.add(notifier.name, notifier.name, summary, True)
                return
            except asyncio.CancelledError:
                raise
            except PermanentNotifyError as e:
                log.error("%s: %s %s not delivered (permanent): %s", notifier.name, name, event.kind, e)
                if record:
                    self.history.add(notifier.name, notifier.name, summary, False, str(e))
                return
            except Exception as e:  # noqa: BLE001
                if attempt == self.max_attempts:
                    log.error("%s: %s %s not delivered after %d tries: %s",
                              notifier.name, name, event.kind, attempt, e)
                    if record:
                        self.history.add(notifier.name, notifier.name, summary, False, str(e))
                    return
                delay = min(60.0, self.retry_base_delay * 2 ** (attempt - 1))
                log.warning("%s: %s %s failed (%s), retry %d in %.0fs",
                            notifier.name, name, event.kind, e, attempt, delay)
                await asyncio.sleep(delay)

    def alert(self, text: str) -> None:
        """A warning for whoever runs this, on every channel that takes alerts (Discord).
        Doesn't wait; call it from inside the event loop."""
        for n in self.notifiers:
            if callable(getattr(n, "alert", None)):
                task = asyncio.get_running_loop().create_task(self._alert(n, text))
                self._alerts.add(task)
                task.add_done_callback(self._alerts.discard)

    async def _alert(self, notifier: Notifier, text: str) -> None:
        try:
            await notifier.alert(text)  # type: ignore[attr-defined]
            self.history.add(notifier.name, "提醒", text, True)
        except Exception as e:  # noqa: BLE001
            log.error("%s: alert not delivered: %s", notifier.name, e)
            self.history.add(notifier.name, "提醒", text, False, str(e))

    async def notify(self, event: StreamEvent) -> None:
        """Send one event now (bypassing the rules) and wait until delivered."""
        self._enqueue(event)
        await self.drain()

    async def drain(self) -> None:
        """Wait until every queued event has been delivered (or given up)."""
        if self._queue is not None:
            await self._queue.join()

    async def aclose(self, timeout: float = 10) -> None:
        # Pending "ended" windows are dropped here, but the stream is still saved as
        # live, so the ended event is sent after the next start if it's still over.
        for t in self._pending_end.values():
            t.cancel()
        self._pending_end.clear()
        if self._alerts:
            await asyncio.wait(self._alerts, timeout=timeout)
        try:
            await asyncio.wait_for(self.drain(), timeout)
        except TimeoutError:
            log.warning("notify queue not empty at shutdown; %d event(s) dropped",
                        self._queue.qsize() if self._queue else 0)
        if self._worker:
            self._worker.cancel()
        for n in self.notifiers:
            await n.aclose()

    # ---- persistence ----

    def _save_state(self) -> None:
        if not self._state:
            return
        try:
            self._state.save(SavedLives(self._live, self._sitting))
        except Exception:  # noqa: BLE001
            log.exception("could not save %s", self.state_file)


def same_address(a: StreamStatus, b: StreamStatus) -> bool:
    """Both lives are watched at the same known address (a channel page, not a video)."""
    return a.url is not None and a.url == b.url


@dataclass
class SavedLives:
    """What the live-stream state file holds."""

    # streamer id -> current live (None: known not live); a missing key: never seen.
    live: dict[str, StreamStatus | None] = field(default_factory=dict)
    # streamer id -> the first live of its current sitting (the live itself until the
    # sitting continues past it; only then is it written to the file). Only for
    # streamers that are live.
    sittings: dict[str, StreamStatus] = field(default_factory=dict)


def decode_live_state(raw: Any) -> SavedLives:
    """The live-stream state file's JSON -> the saved lives; raises if it isn't that shape.
    The optional "sittings" entry is ignored by versions that don't know it."""
    if not isinstance(raw, dict) or not isinstance(streamers := raw.get("streamers", {}), dict):
        raise ValueError("expected {\"streamers\": {streamer id: status or null}}")
    if not isinstance(sittings := raw.get("sittings", {}), dict) or not all(
            isinstance(v, dict) for v in sittings.values()):
        raise ValueError("expected {\"sittings\": {streamer id: status}}")
    live = {sid: StreamStatus.from_dict(v) if v else None for sid, v in streamers.items()}
    # A live streamer without an entry is in a sitting that is still its first live.
    return SavedLives(live, {sid: StreamStatus.from_dict(sittings[sid]) if sid in sittings else current
                             for sid, current in live.items() if current is not None})


def encode_live_state(saved: SavedLives) -> dict[str, Any]:
    raw: dict[str, Any] = {"version": 1,
                           "streamers": {k: v.to_dict() if v else None for k, v in saved.live.items()}}
    sittings = {sid: first.to_dict() for sid, first in saved.sittings.items()
                if (current := saved.live.get(sid)) is not None and first is not current}
    if sittings:
        raw["sittings"] = sittings
    return raw
