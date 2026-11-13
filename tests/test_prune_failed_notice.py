"""The notice that old archives could not be deleted after a download (ticket 11), as Discord
and the QQ groups show it, and as a kind the 通知 page offers. When it is sent: next to
ticket 09's tests in tests/test_download_stage_unexpected_error.py."""

from __future__ import annotations

import asyncio

from backend.monitor import LiveState, StreamStatus
from backend.notify import EventKind, StreamEvent
from backend.notify.discord import DiscordNotifier

from .test_notify_api import app as notify_app
from .test_qq import FakeQQ
from .test_qq import notifier as qq_notifier
from .twitcasting_fakes import MIKA, START

DETAIL = "旧录播没有删掉；这场新录播不受影响。\n```PermissionError: busy```"


def notice(kind: EventKind = EventKind.PRUNE_FAILED) -> StreamEvent:
    return StreamEvent(kind, StreamStatus(MIKA, LiveState.LIVE, title="Night chat", started_at=START),
                       detail=DETAIL)


def test_discord_shows_the_notice_as_a_warning_and_not_as_a_failed_download():
    d = DiscordNotifier("https://example.invalid")
    payload = d.build_payload(notice())
    failure = d.build_payload(notice(EventKind.DOWNLOAD_FAILED))
    downloaded = d.build_payload(notice(EventKind.DOWNLOADED))
    asyncio.run(d.aclose())

    (embed,) = payload["embeds"]
    assert payload["content"] == "⚠️ **Mika** 旧录播没能删除"
    assert embed["description"] == DETAIL
    assert embed["footer"]["text"] == "切片小助手 自动下载"
    assert embed["color"] not in (failure["embeds"][0]["color"], downloaded["embeds"][0]["color"])
    assert payload["allowed_mentions"] == {"parse": []}


def test_only_qq_groups_that_ticked_the_notice_get_it(tmp_path):
    fake = FakeQQ()
    chosen = {"openid": "G1", "name": "章鱼烧", "enabled": True, "streamers": ["Mika"],
              "kinds": ["downloaded", "prune_failed"]}
    n = qq_notifier(tmp_path, fake, chosen, {**chosen, "openid": "G2", "kinds": ["downloaded"]})
    asyncio.run(n.send(notice()))
    asyncio.run(n.aclose())

    (text,) = fake.texts("G1")
    assert text.splitlines()[0] == "⚠️ **Mika** 旧录播没能删除"
    assert "旧录播没有删掉；这场新录播不受影响。" in text.splitlines()
    assert text.splitlines()[-1] == "切片小助手 自动下载"
    assert fake.texts("G2") == []


def test_the_notify_page_offers_the_notice_but_a_new_group_does_not_get_it(tmp_path, monkeypatch):
    client = notify_app(tmp_path, monkeypatch)
    body = client.get("/api/notify").json()
    assert {"id": "prune_failed", "label": "旧录播没能删除"} in body["kinds"]
    assert "prune_failed" not in body["groups"][0]["kinds"]
    r = client.patch("/api/notify/groups", params={"id": "G1"}, json={"kinds": ["downloaded", "prune_failed"]})
    assert r.status_code == 200
    assert client.get("/api/notify").json()["groups"][0]["kinds"] == ["downloaded", "prune_failed"]
