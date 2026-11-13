"""A live whose replay is not published: the automatic download, handed the ended live with
a fake clock, the fake TwitCasting site and the fake yt-dlp (tests/twitcasting_fakes.py),
tells the owner once and keeps checking, or stays silent for a short live; and the notice
as Discord and QQ show it."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from backend import check_state
from backend.downloader.auto import AutoDownloader
from backend.monitor import LiveState, StreamStatus
from backend.monitor.twitcasting import channel_url
from backend.notify import EventKind, StreamEvent
from backend.notify.discord import DiscordNotifier

from .test_notify_api import app as notify_app
from .test_qq import FakeQQ
from .test_qq import notifier as qq_notifier
from .twitcasting_fakes import CHANNEL, MIKA, MOVIE, REPLAY, START, replay_page, tc  # noqa: F401 (tc: fixture)

HOUR = 3600
NOTICE = "回放未公开，12 小时内公开会自动下载。"


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def auto(tc, clock: Clock | None = None, sent: list[StreamEvent] | None = None, **kw):
    clock = clock or Clock()
    sent = [] if sent is None else sent
    ad = AutoDownloader(tc.settings, [MIKA], {MIKA.id: 6}, tc.tmp / "queue.json",
                        retry_interval=900, give_up=12 * HOUR, max_errors=3, notify=sent.append, clock=clock, **kw)
    return ad, clock, sent


def ended(lasted: timedelta | None = timedelta(hours=1), started_at: datetime | None = START) -> StreamEvent:
    """What the notifier hands over when a TwitCasting live ended after `lasted`."""
    status = StreamStatus(MIKA, LiveState.LIVE, title="Night chat", url=channel_url(CHANNEL),
                          stream_id=MOVIE, started_at=started_at)
    end = (started_at or START) + lasted if lasted is not None else None
    return StreamEvent(EventKind.OFFLINE, status, ended_at=end)


def attempt(ad) -> None:
    job = ad.next_due()
    assert job is not None
    ad.finish(job, ad.attempt(job))


def unpublished(tc) -> None:
    tc.site.pages[REPLAY] = replay_page("unpublished")


def test_the_notice_is_sent_on_the_first_check_and_never_again_also_after_a_reload(tc):
    unpublished(tc)
    ad, clock, sent = auto(tc)
    ad.on_ended(ended())
    attempt(ad)
    clock.t += 900
    attempt(ad)
    assert [e.detail for e in sent] == [NOTICE]

    ad, clock, sent = auto(tc, clock)  # a restart: the queue comes back from disk
    clock.t += 900
    attempt(ad)
    assert sent == []
    assert len(ad.load()) == 1  # still checking


def test_a_live_shorter_than_five_minutes_is_dropped_at_once_without_a_message(tc):
    unpublished(tc)
    ad, clock, sent = auto(tc)
    ad.on_ended(ended(lasted=timedelta(minutes=4, seconds=59)))
    attempt(ad)
    assert sent == []
    assert ad.load() == []
    assert len(tc.site.requests) == 1  # not retried
    assert not (tc.tmp / "dl").exists()  # nothing stored


def test_a_live_of_exactly_five_minutes_counts_as_long(tc):
    unpublished(tc)
    ad, clock, sent = auto(tc)
    ad.on_ended(ended(lasted=timedelta(minutes=5)))
    attempt(ad)
    assert [e.kind for e in sent] == [EventKind.REPLAY_UNPUBLISHED]
    assert len(ad.load()) == 1


def test_a_live_without_a_start_time_counts_as_long(tc):
    unpublished(tc)
    ad, clock, sent = auto(tc)
    ad.on_ended(ended(lasted=timedelta(minutes=1), started_at=None))
    attempt(ad)
    assert [e.detail for e in sent] == [NOTICE]
    assert len(ad.load()) == 1


def test_a_short_live_whose_replay_is_published_is_downloaded(tc):
    ad, clock, sent = auto(tc)
    ad.on_ended(ended(lasted=timedelta(minutes=1)))
    attempt(ad)
    assert [e.kind for e in sent] == [EventKind.DOWNLOADED, EventKind.READY]


def test_a_replay_published_before_the_give_up_time_is_downloaded_and_transcribed(tc):
    unpublished(tc)
    transcribed = []
    ad, clock, sent = auto(tc, transcribe=transcribed.append)
    ad.on_ended(ended())
    attempt(ad)
    assert ad.load()[0].next_try == clock.t + 900  # the usual interval

    clock.t += 11 * HOUR
    tc.site.pages[REPLAY] = replay_page("published")
    attempt(ad)
    assert [e.kind for e in sent] == [EventKind.REPLAY_UNPUBLISHED, EventKind.DOWNLOADED]
    assert ad.load()[0].stage == "transcribe"
    attempt(ad)
    assert transcribed and transcribed[0].name.endswith("Night chat 013044.mp4")
    assert [e.kind for e in sent][-1] is EventKind.READY


def test_at_the_give_up_time_the_job_goes_without_a_message_and_leaves_nothing_on_disk(tc):
    unpublished(tc)
    ad, clock, sent = auto(tc)
    ad.on_ended(ended())
    while clock.t < 1_000_000 + 13 * HOUR and ad.load():
        attempt(ad)
        clock.t += 900
    assert ad.load() == []
    assert [e.kind for e in sent] == [EventKind.REPLAY_UNPUBLISHED]
    assert len(tc.site.requests) == 12 * 4 + 1  # every 15 minutes, then once at the give-up time
    assert not (tc.tmp / "dl").exists()


# A job as the version before this one wrote it: no end time, no notice flag.
OLD_JOB = {
    "status": {"name": "Mika", "platform": "twitcasting", "channel": CHANNEL, "state": "live",
               "title": "Night chat", "url": channel_url(CHANNEL), "stream_id": MOVIE,
               "started_at": START.isoformat(), "viewers": None, "thumbnail": None, "avatar": None,
               "error": None, "checked_at": START.isoformat()},
    "queued_at": 1_000_000.0, "next_try": 1_000_000.0, "attempts": 0, "errors": 0, "last_error": "",
    "stage": "download", "video": None,
}


def test_a_queue_file_written_by_the_previous_version_loads_and_its_job_counts_as_a_long_live(tc):
    (tc.tmp / "queue.json").write_text(json.dumps([OLD_JOB]), encoding="utf-8")
    unpublished(tc)
    ad, clock, sent = auto(tc)
    (job,) = ad.load()
    assert (job.status.stream_id, job.queued_at, job.stage) == (MOVIE, 1_000_000.0, "download")
    assert sorted(p.name for p in tc.tmp.iterdir() if p.name.startswith("queue")) == ["queue.json"]  # no backup

    attempt(ad)
    assert [e.detail for e in sent] == [NOTICE]


def test_the_deploy_check_passes_on_a_queue_file_of_the_previous_version(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "download_queue.json").write_text(json.dumps([OLD_JOB]), encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "Mika", "platform": "twitcasting", "channel": CHANNEL}],
                    "streamers_file": str(data / "streamers.json")},
        "notify": {"discord": {"enabled": False}, "state_file": str(data / "notify_state.json"),
                   "log_file": str(data / "notify_log.json")},
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(data / "download_queue.json")},
                     "chat_retry": {"state_file": str(data / "pending_chat.json")}},
    }), encoding="utf-8")

    assert check_state.check(cfg) == ([], 1)


# ---- the notice on Discord and QQ ----

def notice() -> StreamEvent:
    return StreamEvent(EventKind.REPLAY_UNPUBLISHED, ended().status, detail=NOTICE)


def test_discord_shows_the_notice_with_the_automatic_download_footer_and_not_as_a_failure():
    d = DiscordNotifier("https://example.invalid")
    payload = d.build_payload(notice())
    failure = d.build_payload(StreamEvent(EventKind.DOWNLOAD_FAILED, ended().status, detail="boom"))
    asyncio.run(d.aclose())

    (embed,) = payload["embeds"]
    assert "Mika" in payload["content"] and "回放未公开" in payload["content"]
    assert "❗" not in payload["content"] and "失败" not in payload["content"]
    assert embed["description"] == NOTICE
    assert embed["footer"]["text"] == "切片小助手 自动下载"
    assert embed["color"] != failure["embeds"][0]["color"]
    assert payload["allowed_mentions"] == {"parse": []}


def test_qq_groups_that_chose_the_notice_get_it(tmp_path):
    fake = FakeQQ()
    chosen = {"openid": "G1", "name": "章鱼烧", "enabled": True, "streamers": ["Mika"],
              "kinds": ["downloaded", "replay_unpublished"]}
    n = qq_notifier(tmp_path, fake, chosen, {**chosen, "openid": "G2", "kinds": ["downloaded"]})
    asyncio.run(n.send(notice()))
    asyncio.run(n.aclose())

    (text,) = fake.texts("G1")
    assert text.splitlines()[0] == "⏳ **Mika** 回放未公开"
    assert NOTICE in text.splitlines()
    assert text.splitlines()[-1] == "切片小助手 自动下载"
    assert fake.texts("G2") == []


def test_the_notify_page_offers_the_notice_as_a_kind_a_group_can_choose(tmp_path, monkeypatch):
    client = notify_app(tmp_path, monkeypatch)
    assert {"id": "replay_unpublished", "label": "回放未公开"} in client.get("/api/notify").json()["kinds"]
    r = client.patch("/api/notify/groups", params={"id": "G1"}, json={"kinds": ["downloaded", "replay_unpublished"]})
    assert r.status_code == 200
    assert client.get("/api/notify").json()["groups"][0]["kinds"] == ["downloaded", "replay_unpublished"]
