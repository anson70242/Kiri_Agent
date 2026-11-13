"""An exception that is not a download error in the download stage (a bug, an unexpected
page, a file problem) is one failed attempt like any other: the automatic download, handed
an ended TwitCasting live, with a fake clock, the fake site and a fake yt-dlp that breaks
(tests/twitcasting_fakes.py)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from backend.archive import Downloads
from backend.downloader.auto import AutoDownloader
from backend.monitor import LiveState, StreamStatus
from backend.monitor.twitcasting import channel_url
from backend.notify import EventKind, StreamEvent

from .twitcasting_fakes import CHANNEL, MIKA, MOVIE, START, FakeYtdlp, tc  # noqa: F401 (tc: fixture)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def auto(tc):
    clock = Clock()
    sent: list[StreamEvent] = []
    ad = AutoDownloader(tc.settings, [MIKA], {MIKA.id: 6}, tc.tmp / "queue.json",
                        retry_interval=900, give_up=12 * 3600, max_errors=3, notify=sent.append, clock=clock)
    status = StreamStatus(MIKA, LiveState.LIVE, title="Night chat", url=channel_url(CHANNEL),
                          stream_id=MOVIE, started_at=START)
    ad.on_ended(StreamEvent(EventKind.OFFLINE, status, ended_at=START + timedelta(hours=1)))
    return ad, clock, sent


def break_ytdlp(monkeypatch, when: str) -> None:
    """yt-dlp raises ValueError on its metadata call ("info") or while downloading ("video")."""
    real = FakeYtdlp.extract_info

    def extract_info(self, url, download=True):
        if download == (when == "video"):
            raise ValueError("unexpected data from yt-dlp")
        return real(self, url, download)

    monkeypatch.setattr(FakeYtdlp, "extract_info", extract_info)


def run_worker_once(ad) -> None:
    """The download worker's pass over the due jobs, as it runs in the service."""
    asyncio.run(ad.run_due("download"))


def test_an_unexpected_exception_is_one_failed_attempt_retried_at_the_usual_interval(tc, monkeypatch, caplog):
    break_ytdlp(monkeypatch, "info")
    ad, clock, sent = auto(tc)
    with caplog.at_level(logging.WARNING):
        run_worker_once(ad)

    (job,) = ad.load()
    assert (job.attempts, job.errors) == (1, 1)
    assert job.next_try == clock.t + 900
    assert "ValueError" in job.last_error and "unexpected data from yt-dlp" in job.last_error
    assert sent == []
    assert any(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)  # the traceback is logged


def test_after_the_usual_number_of_errors_the_job_is_given_up_with_the_usual_failure_message(tc, monkeypatch):
    break_ytdlp(monkeypatch, "info")
    ad, clock, sent = auto(tc)
    for _ in range(3):
        run_worker_once(ad)
        clock.t += 900

    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOAD_FAILED]
    assert sent[0].detail.startswith("试了 3 次仍失败")
    assert "ValueError: unexpected data from yt-dlp" in sent[0].detail


def test_the_now_downloading_entry_is_cleared_after_an_exception_while_downloading(tc, monkeypatch):
    break_ytdlp(monkeypatch, "video")
    ad, clock, sent = auto(tc)
    run_worker_once(ad)

    assert ad.snapshot()["active"] == []
    (job,) = ad.load()
    assert job.errors == 1


def test_a_cancelled_worker_still_stops(tc, monkeypatch):
    def cancelled(self, url, download=True):
        raise asyncio.CancelledError

    monkeypatch.setattr(FakeYtdlp, "extract_info", cancelled)
    ad, clock, sent = auto(tc)
    try:
        run_worker_once(ad)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("the cancellation was swallowed")
    (job,) = ad.load()
    assert (job.attempts, job.errors) == (0, 0)


def test_a_failing_cleanup_of_old_archives_does_not_fail_the_download(tc, monkeypatch, caplog):
    def broken_prune(self, protect, keep):
        raise PermissionError("old archive is open in another program")

    monkeypatch.setattr(Downloads, "prune", broken_prune)
    ad, clock, sent = auto(tc)
    transcribed = []
    ad.transcribe = transcribed.append
    with caplog.at_level(logging.WARNING):
        run_worker_once(ad)

    assert [e.kind for e in sent] == [EventKind.DOWNLOADED]
    assert "清理" not in sent[0].detail  # nothing reported as pruned
    (job,) = ad.load()
    assert (job.stage, job.errors) == ("transcribe", 0)
    assert any(r.exc_info and r.exc_info[0] is PermissionError for r in caplog.records)
