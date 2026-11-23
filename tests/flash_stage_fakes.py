"""The pipeline's flash stage with a fake flash service: the fake (an object with the flash
service client's interface, scripted per test) and the builders the flash stage's tests share.

    ad, clock, sent, folder = processed_live(tmp_path)    # A's live, processed: its flash job is next
    run_flash(ad, clock)                                  # the flash stage until its jobs are over
    assert links(ready(sent)) == flash_ready_links()

Made-up values only: the flash links are `https://flash.example/...`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import backend.downloader
from backend.archive import Archive, Downloads
from backend.downloader.auto import Outcome
from backend.downloader.base import DownloadResult, VideoInfo
from backend.downloader.pending import PendingChats
from backend.flash.client import AccountState, Progress, StartedTransfer
from backend.notify import EventKind, StreamEvent

from .helpers import A, bare_archive, later, live, sample_archive
from .test_drive_stage import SITE, ChatSource, downloaded, make, step

# The flash link's expiry the fake answers: 2026/12/7 in Hong Kong, still 2026/12/6 in UTC.
EXPIRES = datetime(2026, 12, 6, 18, 30, tzinfo=timezone.utc)
MADE = datetime(2026, 11, 22, 18, 30, tzinfo=timezone.utc)
FLASH_LABEL = "闪传链接（影片、弹幕、中日字幕 SRT、路灯表）"


def flash_link(n: int = 1) -> str:
    """The flash link of the fake's `n`-th flash transfer."""
    return f"https://flash.example/s/fs-{n}"


FLASH_LINK = flash_link(1)


def transfer(n: int = 1) -> StartedTransfer:
    """The fake's `n`-th flash transfer, as the flash service answers its start."""
    return StartedTransfer(f"fs-{n}", flash_link(n), EXPIRES, MADE)


def uploading(sent: int = 50, total: int = 100) -> Progress:
    return Progress(sent, total, done=0, failed=0, finished=False)


def finished(total: int = 100, files: int = 2) -> Progress:
    return Progress(total, total, done=files, failed=0, finished=True)


class FakeFlash:
    """The flash service client as the pipeline uses it. `progress` answers each look at a
    flash transfer's progress in turn (a Progress, or an exception to raise), the last one
    again and again; finished at once by default. `start` answers each start in turn (a
    StartedTransfer or an exception); without it the n-th start answers `transfer(n)`.

    Records `starts` (the name and the files' names of each start), `existed` (whether every
    file was there at that start) and `looks` (the id of each look at a progress)."""

    def __init__(self, *progress: Progress | Exception, start: list[StartedTransfer | Exception] | None = None):
        self.progress_answers = list(progress) or [finished()]
        self.start_answers = list(start or [])
        self.starts: list[tuple[str, list[str]]] = []
        self.existed: list[bool] = []
        self.looks: list[str] = []
        self.during_start = None  # called inside a start, with its files

    def account(self) -> AccountState:
        return AccountState("online")

    def require_online(self) -> None:
        """Online: returns."""

    def start(self, files: list[Path], name: str) -> StartedTransfer:
        self.starts.append((name, [f.name for f in files]))
        self.existed.append(all(f.is_file() for f in files))
        if self.during_start:
            self.during_start(files)
        if self.start_answers:
            answer = self.start_answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return transfer(len(self.starts))

    def progress(self, transfer_id: str) -> Progress:
        self.looks.append(transfer_id)
        answer = self.progress_answers.pop(0) if len(self.progress_answers) > 1 else self.progress_answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


_NEW = object()


def flash_pipeline(tmp_path, flash=_NEW, drive=None, ticked=(A,), folders=None, **kw):
    """The pipeline of `make` (A and B, a fake clock, the sent events) with the fake flash
    service `flash` (a new FakeFlash by default; None: no flash service is configured) and the
    streamers `ticked` for flash transfers. Drive copies are off unless `folders` ticks some
    (and `drive` is given). Returns (ad, clock, sent)."""
    ad, clock, sent = make(tmp_path, drive, folders={} if folders is None else folders,
                           flash=FakeFlash() if flash is _NEW else flash, **kw)
    ad.flash_streamers = {s.id for s in ticked}
    return ad, clock, sent


def processed_live(tmp_path, flash=_NEW, s=A, full=False, **kw):
    """`s`'s live downloaded and transcribed in the pipeline of `flash_pipeline`: what follows
    the processing is next. Its archive (`<s>/20261110/歌枠`, stem `v`) has a video and a chat;
    `full` adds both transcripts and the highlight table, so that a flash transfer has files
    to write into its staging folder. Returns (ad, clock, sent, the archive's folder)."""
    ad, clock, sent = flash_pipeline(tmp_path, flash, **kw)
    folder = downloaded(ad, s)
    if full:
        sample_archive(ad.settings.output_dir, s.name, "20261110", "歌枠", stem="v", video=b"x")
    step(ad, "transcribe")
    return ad, clock, sent, folder


def late_chat_live(tmp_path, flash=_NEW, **kw):
    """A's live downloaded without its chat (queued for a retry) and transcribed, in the
    pipeline of `flash_pipeline`. Bring the chat in with `chat_comes_in`. Returns (ad, clock,
    sent, the archive's folder)."""
    ad, clock, sent = flash_pipeline(tmp_path, flash, **kw)
    ad.on_ended(StreamEvent(EventKind.OFFLINE, live(A, sid="v1"), later(60)))
    folder = bare_archive(ad.settings.output_dir, "A", "20261110", "歌枠", stem="v", video=b"x")
    info = VideoInfo("youtube", "v1", "https://x/v1", "title v1", "@a", "A", "20261110")
    PendingChats.from_settings(ad.settings).add(info, folder, "chat replay not available yet")
    ad.finish(ad.next_due("download"),
              Outcome("done", result=DownloadResult(info, folder, video=Archive.from_folder(folder).video,
                                                    chat_queued=True)))
    step(ad, "transcribe")
    return ad, clock, sent, folder


def chat_comes_in(ad, monkeypatch) -> None:
    """The late chat of `late_chat_live` is downloaded by the chat retry, and the download
    worker's pass after it runs."""
    monkeypatch.setattr(backend.downloader, "downloader_for", lambda *a: ChatSource(True))
    PendingChats.from_settings(ad.settings).retry([A], ad.settings, force=True)
    asyncio.run(ad.retry_chats())


def run_flash(ad, clock, limit: int = 50) -> None:
    """Run the flash stage until it has no job left, moving the clock as long as the stage's
    worker would sleep before each step."""
    for _ in range(limit):
        if not any(j.stage == "flash" for j in ad.load()):
            return
        if ad.next_due("flash") is None:
            clock.t += ad.sleep_time("flash")
        else:
            step(ad, "flash")
    raise AssertionError(f"the flash stage still has jobs after {limit} steps")


def stages(ad) -> list[str]:
    return [j.stage for j in ad.load()]


def ready(sent):
    """The one "全部处理完成" sent."""
    (event,) = [e for e in sent if e.kind is EventKind.READY]
    return event


def flash_ready_links(creator: str = "A", link: str = FLASH_LINK) -> list[tuple[str, str]]:
    """"全部处理完成"'s links with a flash link, for `<creator>/20261110/歌枠`: the flash link,
    then the page."""
    ref = f"{creator}%2F20261110%2F%E6%AD%8C%E6%9E%A0"
    return [(link, FLASH_LABEL), (f"{SITE}/archives#{ref}", "录播页面")]


def staging(tmp_path, creator: str = "A") -> Path:
    """The staging folder of `<creator>/20261110/歌枠` in the downloads folder of
    `flash_pipeline(tmp_path, ...)`."""
    return Downloads(tmp_path / "dl").flash_staging / creator / "20261110" / "歌枠"


# ---- a failed flash transfer (ticket 09) ----

PAUSE = 900  # the pipeline's retry pause in the tests' pipeline (`make`: retry_interval=900)


def alerts_of(ad) -> list[str]:
    """The pipeline's operator alerts (the owner's Discord messages), collected from now on."""
    alerts: list[str] = []
    ad.alert = alerts.append
    return alerts


def refused(n: int = 3, reason: str = "闪传服务拒绝了") -> FakeFlash:
    """A fake flash service that refuses the first `n` starts, then starts and finishes."""
    from backend.flash.client import FlashFailed

    return FakeFlash(start=[FlashFailed(reason) for _ in range(n)])


def staging_exists(ad) -> bool:
    """Whether any staging folder of a flash transfer is on disk."""
    from backend.archive import Downloads

    return Downloads(ad.settings.output_dir).flash_staging.exists()


# ---- the flash account offline (ticket 10) ----

HOUR = 3600


class AccountFlash(FakeFlash):
    """A FakeFlash whose flash account can be offline: `state` ("online", "offline" or
    "unreachable", offline by default) is what `account()` answers, and while it is not
    online a start or a look at a progress raises FlashUnavailable, as the client does
    (nothing is started then). Records `accounts`: how often the pipeline asked whether
    the account is online (`require_online`, as the client answers it from the flash service
    alone; `account()`, which the 通知 page uses, is not counted)."""

    def __init__(self, *progress: Progress | Exception, state: str = "offline", **kw):
        super().__init__(*progress, **kw)
        self.state = state
        self.accounts = 0
        # False: the status answers as `state` says, but a start or a look at a progress
        # cannot reach the flash service (FlashUnavailable), however online the status says
        # the account is. `unserved` counts those calls.
        self.serving = True
        self.unserved = 0

    def account(self) -> AccountState:
        return AccountState(self.state)  # type: ignore[arg-type]

    def require_online(self) -> None:
        self.accounts += 1
        self._online()

    def _online(self) -> None:
        from backend.flash.client import FlashUnavailable

        if self.state != "online":
            raise FlashUnavailable("闪传账号掉线" if self.state == "offline" else "连不上闪传服务（ConnectionRefusedError）")

    def _served(self) -> None:
        from backend.flash.client import FlashUnavailable

        self._online()
        if not self.serving:
            self.unserved += 1
            raise FlashUnavailable("连不上闪传服务（TimeoutError）")

    def start(self, files: list[Path], name: str) -> StartedTransfer:
        self._served()
        return super().start(files, name)

    def progress(self, transfer_id: str) -> Progress:
        self._served()
        return super().progress(transfer_id)


def pass_time(ad, clock, seconds: float) -> None:
    """`seconds` go by; then the flash stage's worker tries every job that is due by then, as
    it does when it wakes up."""
    clock.t += seconds
    for _ in range(50):
        if ad.next_due("flash") is None:
            return
        step(ad, "flash")
    raise AssertionError("the flash stage keeps finding jobs due")
