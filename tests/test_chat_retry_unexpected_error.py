"""An exception that is not a download error while retrying a queued chat (a bug, an
unexpected answer, a file problem) is one failed try for that entry, and the rest of the
pass goes on: the chat retry queue with a fake clock and a fake platform downloader."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest

from backend.downloader import DownloadSettings, PendingChats
from backend.downloader.base import PlatformDownloader, VideoInfo

INTERVAL = 1800
GIVE_UP = 48 * 3600


class BreakingDownloader(PlatformDownloader):
    """A platform whose replay metadata raises ValueError for addresses with "broken" in them."""

    platform = "breaking"
    url_re = re.compile(r"^breaking://")

    def info(self, url):
        if "broken" in url:
            raise ValueError("unexpected answer from the site")
        return VideoInfo("breaking", url.rsplit("/", 1)[-1], url, "Title", "c", "Chan", "20261110",
                         live_status="was_live")

    def download_video(self, info, out_dir: Path, progress=None) -> Path:
        raise AssertionError("the chat retry never downloads a video")

    def download_raw_chat(self, info, out_dir: Path) -> Path:
        p = out_dir / f"{info.stem}.chat.json"  # Twitch layout, so it gets cleaned
        p.write_text(json.dumps({"comments": [{"content_offset_seconds": 61, "commenter": {"display_name": "Bob"},
                                               "message": {"body": "hi"}}]}), encoding="utf-8")
        return p


PlatformDownloader.registry.pop("breaking")  # only registered while a test uses it


@pytest.fixture(autouse=True)
def breaking(monkeypatch):
    monkeypatch.setitem(PlatformDownloader.registry, "breaking", BreakingDownloader)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def queue(tmp_path, *videos: str):
    """A chat retry queue holding the chats of these videos, each due now."""
    clock = Clock()
    settings = DownloadSettings(output_dir=tmp_path / "dl", pending_file=tmp_path / "pending.json",
                                chat_retry_interval=INTERVAL, chat_retry_give_up=GIVE_UP)
    q = PendingChats(settings.pending_file, INTERVAL, GIVE_UP, clock)
    for v in videos:
        q.add(VideoInfo("breaking", v, f"breaking://{v}", "Title", "c", "Chan", "20261110"),
              tmp_path / "out" / v, "chat not ready yet")
    clock.t += INTERVAL
    return q, clock, settings


def test_an_unexpected_exception_is_one_failed_try_retried_at_the_usual_interval(tmp_path, caplog):
    q, clock, settings = queue(tmp_path, "broken")
    with caplog.at_level(logging.WARNING):
        report = q.retry([], settings)

    (e,) = q.load()
    assert e.attempts == 1
    assert e.next_try == clock.t + INTERVAL
    assert "ValueError" in e.last_error and "unexpected answer from the site" in e.last_error
    assert [w.info.id for w in report.waiting] == ["broken"]
    assert any(r.exc_info and r.exc_info[0] is ValueError for r in caplog.records)  # the traceback is logged


def test_the_entry_is_dropped_at_its_give_up_time(tmp_path):
    q, clock, settings = queue(tmp_path, "broken")
    clock.t += GIVE_UP
    report = q.retry([], settings)

    assert [e.info.id for e in report.given_up] == ["broken"]
    assert q.load() == []


def test_the_other_chats_of_the_pass_are_still_fetched_and_the_queue_saved(tmp_path):
    q, clock, settings = queue(tmp_path, "broken", "fine")
    report = q.retry([], settings)

    assert [e.info.id for e, _ in report.done] == ["fine"]
    assert next((tmp_path / "out" / "fine").glob("*.chat_parsed.json")).exists()
    (e,) = q.load()  # the fetched chat left the queue; the broken one is rescheduled
    assert (e.info.id, e.attempts, e.next_try) == ("broken", 1, clock.t + INTERVAL)


def test_a_cancellation_still_stops_the_pass(tmp_path, monkeypatch):
    def cancelled(self, url):
        raise asyncio.CancelledError

    monkeypatch.setattr(BreakingDownloader, "info", cancelled)
    q, clock, settings = queue(tmp_path, "fine")
    with pytest.raises(asyncio.CancelledError):
        q.retry([], settings)
    (e,) = q.load()
    assert e.attempts == 0
