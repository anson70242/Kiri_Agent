"""Retry queue for chat replays that weren't ready yet.

YouTube often publishes a stream's chat replay hours after the stream ends. `download()`
puts such chats here (a JSON file, default `data/pending_chat.json`); `retry()` tries
each due entry again, every `interval` seconds, until it succeeds or `give_up` seconds
have passed since it was queued.

The entry keeps the original VideoInfo, so the chat lands next to the video under the
same file name even if the title changes in the meantime.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.monitor import Streamer
from backend.state_file import StateFile, expect_list

from .base import ChatNotReady, DownloadError, DownloadSettings, VideoInfo

log = logging.getLogger(__name__)


@dataclass
class PendingChat:
    info: VideoInfo
    output_dir: Path
    queued_at: float
    next_try: float
    attempts: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"info": asdict(self.info), "output_dir": str(self.output_dir)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PendingChat:
        return cls(**d | {"info": VideoInfo(**d["info"]), "output_dir": Path(d["output_dir"])})


def decode_pending_chats(raw: Any) -> list[PendingChat]:
    """The chat retry queue file's JSON -> entries; raises if it isn't a list of entries."""
    return [PendingChat.from_dict(d) for d in expect_list(raw, "queued chats")]


@dataclass
class RetryReport:
    done: list[tuple[PendingChat, Path]] = field(default_factory=list)
    waiting: list[PendingChat] = field(default_factory=list)  # failed again, still queued
    given_up: list[PendingChat] = field(default_factory=list)


class PendingChats:
    def __init__(
        self,
        path: Path,
        interval: float = 30 * 60,
        give_up: float = 48 * 3600,
        clock: Callable[[], float] = time.time,
    ):
        # Runtime state: a damaged queue is set aside as a backup (the owner is alerted).
        self._store = StateFile(path, default=list, policy="recover", decode=decode_pending_chats,
                                encode=lambda entries: [e.to_dict() for e in entries])
        self.interval = interval
        self.give_up = give_up
        self.clock = clock

    @classmethod
    def from_settings(cls, settings: DownloadSettings) -> PendingChats | None:
        if not settings.pending_file:
            return None
        return cls(settings.pending_file, settings.chat_retry_interval, settings.chat_retry_give_up)

    # ---- storage ----

    def load(self) -> list[PendingChat]:
        return self._store.load()

    def save(self, entries: Iterable[PendingChat]) -> None:
        self._store.save(list(entries))

    # ---- queue ----

    def add(self, info: VideoInfo, output_dir: Path, error: str) -> None:
        """Queue a chat; re-adding the same video keeps its original queue time."""
        entries = self.load()
        now = self.clock()
        for e in entries:
            if e.info.platform == info.platform and e.info.id == info.id:
                e.next_try, e.last_error = now + self.interval, error
                break
        else:
            entries.append(PendingChat(info, output_dir, now, now + self.interval, 0, error))
            log.info("chat queued for retry: %s", info.title)
        self.save(entries)

    def drop_under(self, folder: Path) -> None:
        """Forget queued chats whose video folder is inside `folder` (it was deleted)."""
        entries = self.load()
        keep = [e for e in entries if not e.output_dir.is_relative_to(folder)]
        if len(keep) != len(entries):
            self.save(keep)

    def next_due(self) -> float | None:
        """Time of the next scheduled retry, None when the queue is empty."""
        return min((e.next_try for e in self.load()), default=None)

    def retry(
        self,
        streamers: Iterable[Streamer],
        settings: DownloadSettings,
        *,
        force: bool = False,
    ) -> RetryReport:
        """Try every due entry (every entry with `force`) once."""
        from . import downloader_for  # here, not at import: the package imports this module

        report = RetryReport()
        keep: list[PendingChat] = []
        for e in self.load():
            now = self.clock()
            if not force and e.next_try > now:
                keep.append(e)
                continue
            e.attempts += 1
            try:
                dl = downloader_for(e.info.url, settings, streamers)
                fresh = dl.info(e.info.url)
                if fresh.live_status in ("is_live", "post_live"):
                    raise ChatNotReady(f"stream is {fresh.live_status}")
                e.output_dir.mkdir(parents=True, exist_ok=True)
                path = dl.download_chat(e.info, e.output_dir)
            except DownloadError as err:  # not-ready or a network blip: both worth retrying
                e.last_error = str(err)
                if now - e.queued_at >= self.give_up:
                    log.warning("giving up on chat for %s: %s", e.info.title, err)
                    report.given_up.append(e)
                else:
                    e.next_try = now + self.interval
                    keep.append(e)
                    report.waiting.append(e)
                continue
            log.info("chat downloaded on attempt %d: %s", e.attempts, path)
            report.done.append((e, path))
        # Entries added by another process while we were retrying would be lost by a
        # plain save; merge them back in.
        handled = {(e.info.platform, e.info.id) for e, _ in report.done} | {
            (e.info.platform, e.info.id) for e in report.given_up
        }
        mine = {(e.info.platform, e.info.id) for e in keep}
        extra = [e for e in self.load() if (k := (e.info.platform, e.info.id)) not in mine and k not in handled]
        self.save(keep + extra)
        return report
