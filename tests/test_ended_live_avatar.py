"""Before the replay of an ended Twitch live is downloaded, its messages carry the streamer's
avatar in place of the live preview, which Twitch turns into a camera once the live is over:
「下播了」, 「回放未公开」 and 「回放下载失败」. While the live is on, the live preview stays."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from backend.downloader import auto as auto_mod
from backend.downloader.base import DownloadError, ReplayUnpublished
from backend.monitor import StreamMonitor
from backend.notify import EventKind, LiveNotifier

from .helpers import LIVE, OFFLINE, A, B, Recorder, later, live, off
from .test_replay_cover import AVATAR, LIVE_PREVIEW, covers, ended, make

GRACE = 0.05


def on_air(streamer=B, avatar=AVATAR):
    """A live of `streamer` as the monitor reports it while it is on: the live preview, the avatar."""
    return dataclasses.replace(live(streamer, sid="v1"), thumbnail=LIVE_PREVIEW, avatar=avatar)


# ---- the live notifier: 「开播了」 and 「下播了」 ----


async def went_live_and_ended(streamer, avatar=AVATAR):
    """`streamer` goes live and ends, through the monitor and the live notifier; what the
    notifier sent, and the dashboard's card of her while she was live."""
    rec = Recorder()
    notifier = LiveNotifier([rec], offline_grace=GRACE)
    async with StreamMonitor([streamer]) as mon:
        mon.on_change.append(notifier.on_change)
        await mon.observe(off(streamer))
        await mon.observe(on_air(streamer, avatar))
        await notifier.drain()
        card = mon.statuses[streamer].to_dict()
        await mon.observe(dataclasses.replace(off(streamer), avatar=avatar, checked_at=later(60)))
        await asyncio.sleep(GRACE * 3)
    await notifier.aclose()
    return [(e.kind, e.status.thumbnail) for e in rec.raw], card


def test_stream_ended_of_a_twitch_live_carries_the_avatar():
    sent, _ = asyncio.run(went_live_and_ended(B))
    assert sent == [(LIVE, LIVE_PREVIEW), (OFFLINE, AVATAR)]


def test_stream_ended_of_a_youtube_live_carries_the_monitors_cover_as_before():
    sent, _ = asyncio.run(went_live_and_ended(A))
    assert sent == [(LIVE, LIVE_PREVIEW), (OFFLINE, LIVE_PREVIEW)]


def test_the_dashboards_live_card_shows_the_live_preview_while_the_streamer_is_live():
    _, card = asyncio.run(went_live_and_ended(B))
    assert card["state"] == "live" and card["thumbnail"] == LIVE_PREVIEW


def test_stream_ended_of_a_twitch_streamer_without_an_avatar_has_no_picture():
    sent, _ = asyncio.run(went_live_and_ended(B, avatar=None))
    assert sent == [(LIVE, LIVE_PREVIEW), (OFFLINE, None)]


# ---- the pipeline, before it has a replay ----


class NoReplay:
    """A replay source that fails with `error` when asked for the live's replay."""

    def __init__(self, error: Exception):
        self.error = error

    def find_replay(self, url, channel, live_id, started_at):
        raise self.error


def before_the_replay(tmp_path, monkeypatch, error, streamer=B, avatar=AVATAR):
    """`streamer`'s live ended and its replay cannot be had (`error`): what the pipeline sent."""
    ad, sent = make(tmp_path, monkeypatch)
    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: NoReplay(error))
    ad.on_ended(ended(streamer, avatar))
    job = ad.next_due()
    ad.finish(job, ad.attempt(job))
    return covers(sent)


@pytest.mark.parametrize("error, kind", [
    (ReplayUnpublished("subscribers only"), EventKind.REPLAY_UNPUBLISHED),
    (DownloadError("video unavailable"), EventKind.DOWNLOAD_FAILED)], ids=["unpublished", "download failed"])
def test_before_the_replay_a_twitch_lives_messages_carry_the_avatar(tmp_path, monkeypatch, error, kind):
    assert before_the_replay(tmp_path, monkeypatch, error) == [(kind, AVATAR)]


def test_a_download_given_up_waiting_for_a_twitch_replay_carries_the_avatar(tmp_path, monkeypatch):
    ad, sent = make(tmp_path, monkeypatch)
    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: NoReplay(AssertionError("unused")))
    monkeypatch.setattr(NoReplay, "find_replay", lambda self, *a: None)  # never listed
    ad.on_ended(ended())
    ad.clock.t += ad.give_up
    job = ad.next_due()
    ad.finish(job, ad.attempt(job))

    assert covers(sent) == [(EventKind.DOWNLOAD_FAILED, AVATAR)]


def test_before_the_replay_a_twitch_streamer_without_an_avatar_gets_no_picture(tmp_path, monkeypatch):
    assert before_the_replay(tmp_path, monkeypatch, ReplayUnpublished("subscribers only"), avatar=None) == [
        (EventKind.REPLAY_UNPUBLISHED, None)]


def test_before_the_replay_a_youtube_lives_messages_carry_the_monitors_cover(tmp_path, monkeypatch):
    assert before_the_replay(tmp_path, monkeypatch, ReplayUnpublished("private"), streamer=A) == [
        (EventKind.REPLAY_UNPUBLISHED, LIVE_PREVIEW)]
