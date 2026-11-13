"""The automatic download of a TwitCasting live's replay once the live ended: handed the
ended live, with a fake clock, the fake site and the fake yt-dlp (tests/twitcasting_fakes.py)."""

from __future__ import annotations

from backend.downloader.auto import AutoDownloader
from backend.monitor import LiveState, StreamStatus
from backend.monitor.twitcasting import channel_url
from backend.notify import EventKind, StreamEvent

from .twitcasting_fakes import CHANNEL, MIKA, MOVIE, REPLAY, START, replay_page, tc  # noqa: F401 (tc: fixture)

HOUR = 3600


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def auto(tc):
    clock = Clock()
    sent: list[StreamEvent] = []
    ad = AutoDownloader(tc.settings, [MIKA], {MIKA.id: 6}, tc.tmp / "queue.json",
                        retry_interval=900, give_up=12 * HOUR, max_errors=3, notify=sent.append, clock=clock)
    return ad, clock, sent


def ended(live_id: str | None = MOVIE) -> StreamEvent:
    """What the notifier hands over when a TwitCasting live ended (the monitor's last status)."""
    status = StreamStatus(MIKA, LiveState.LIVE, title="Night chat", url=channel_url(CHANNEL),
                          stream_id=live_id, started_at=START)
    return StreamEvent(EventKind.OFFLINE, status)


def run_until_settled(ad, clock, rounds: int = 10) -> None:
    """Attempt the job at every retry until it is gone."""
    for _ in range(rounds):
        job = ad.next_due()
        if job is None:
            return
        ad.finish(job, ad.attempt(job))
        clock.t += 900


def test_an_ended_live_downloads_its_replay_found_from_the_channel_and_the_live_id(tc):
    ad, clock, sent = auto(tc)
    ad.on_ended(ended())
    run_until_settled(ad, clock)

    assert [str(q.url) for q in tc.site.requests] == [REPLAY]
    video = tc.tmp / "dl" / "Mika" / "20261006" / "Night chat 013044" / "[20261006][Mika] Night chat 013044.mp4"
    assert video.exists()
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.READY]
    assert video.name in sent[0].detail
    assert "弹幕：失败" in sent[0].detail and "not supported yet" in sent[0].detail
    assert ad.load() == []


def test_a_live_without_an_id_fails_with_a_message_that_says_so(tc):
    ad, clock, sent = auto(tc)
    ad.on_ended(ended(live_id=None))
    run_until_settled(ad, clock)

    assert tc.site.requests == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOAD_FAILED]
    assert "no live id" in sent[0].detail


def test_a_missing_login_gives_up_with_the_twitcasting_cookies_hint(tc):
    tc.site.pages[REPLAY] = replay_page("login")
    ad, clock, sent = auto(tc)
    ad.on_ended(ended())
    run_until_settled(ad, clock)

    assert len(tc.site.requests) == 3  # the usual retries
    assert [e.kind for e in sent] == [EventKind.DOWNLOAD_FAILED]
    assert "data/twitcasting_cookies.txt" in sent[0].detail
    assert "youtube_cookies" not in sent[0].detail


def test_a_missing_cookies_file_gives_up_with_the_twitcasting_cookies_hint(tc):
    tc.cookies.unlink()
    ad, clock, sent = auto(tc)
    ad.on_ended(ended())
    run_until_settled(ad, clock)

    assert [e.kind for e in sent] == [EventKind.DOWNLOAD_FAILED]
    assert "data/twitcasting_cookies.txt" in sent[0].detail


def test_a_password_protected_live_fails_saying_it_is_not_supported(tc):
    tc.site.pages[REPLAY] = replay_page("password")
    ad, clock, sent = auto(tc)
    ad.on_ended(ended())
    run_until_settled(ad, clock)

    assert [e.kind for e in sent] == [EventKind.DOWNLOAD_FAILED]
    assert "不支持" in sent[0].detail
    assert "cookies" not in sent[0].detail


def test_a_replay_that_is_not_published_waits_like_one_not_ready_and_is_given_up(tc):
    tc.site.pages[REPLAY] = replay_page("unpublished")
    ad, clock, sent = auto(tc)
    ad.on_ended(ended())
    job = ad.next_due()
    ad.finish(job, ad.attempt(job))
    assert sent == [] and ad.load()[0].errors == 0  # waiting, not failing

    clock.t += 12 * HOUR
    job = ad.next_due()
    ad.finish(job, ad.attempt(job))
    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.DOWNLOAD_FAILED]
    assert "仍未就绪" in sent[0].detail
