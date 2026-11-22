"""The drive's messages: "☁️ 网盘上传完成", "❗ 网盘上传失败" and "⚠️ 网盘授权失效" on Discord and
to the QQ groups that tick them, and "网盘上传完成" showing its drive link like any other link."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.notify import EventKind, Link, StreamEvent
from backend.notify.discord import DiscordNotifier
from backend.notify.qq import QQGroups

from .helpers import A, live
from .test_notify_api import app
from .test_qq import COVER, SUBSCRIBED, FakeQQ, notifier, send

DRIVE_LINK = "https://drive.example/s/abc?pwd=x1y2"
UPLOADED = StreamEvent(EventKind.DRIVE_UPLOADED, live(A, thumbnail=COVER),
                       detail="网盘路径：`录播/A/v.zip`\n没有网盘链接：分享被拒绝")
WITH_LINK = StreamEvent(EventKind.DRIVE_UPLOADED, live(A), detail="日期：2026-11-10",
                        links=(Link("网盘链接", DRIVE_LINK),))


def discord(event: StreamEvent) -> dict:
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(204)

    n = DiscordNotifier("https://discord.test/hook", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(n.send(event))
    (payload,) = sent
    return payload


def test_drive_copy_uploaded_shows_the_drive_link_like_any_link():
    assert discord(WITH_LINK)["embeds"][0]["description"] == f"日期：2026-11-10\n[网盘链接]({DRIVE_LINK})"


def test_drive_copy_uploaded_shows_the_drive_link_like_any_link_on_qq(tmp_path):
    fake = FakeQQ(cover_ok=False)
    send(notifier(tmp_path, fake, {**SUBSCRIBED, "kinds": ["drive_uploaded"]}), WITH_LINK)
    (text,) = fake.texts("G1")
    assert text == ("☁️ **A** 网盘上传完成\ntitle v1\n日期：2026-11-10\n"
                    f"[网盘链接]({DRIVE_LINK})\n切片小助手 自动下载")


FAILED = StreamEvent(EventKind.DRIVE_FAILED, live(A, thumbnail=COVER), detail="网盘上传失败，之后会自动重试：\n32003 空间不足")
LAPSED = StreamEvent(EventKind.DRIVE_AUTHORIZATION_LAPSED, live(A, thumbnail=COVER),
                     detail="夸克网盘的授权失效了")
DRIVE_EVENTS = [(UPLOADED, "☁️", "网盘上传完成"), (FAILED, "❗", "网盘上传失败"), (LAPSED, "⚠️", "网盘授权失效")]


@pytest.mark.parametrize("event, icon, text", DRIVE_EVENTS, ids=lambda x: getattr(x, "kind", x))
def test_every_drive_message_goes_to_discord_with_its_headline(event, icon, text):
    payload = discord(event)
    assert payload["content"] == f"{icon} **A** {text}"
    assert "image" not in payload["embeds"][0]


@pytest.mark.parametrize("event, icon, text", DRIVE_EVENTS, ids=lambda x: getattr(x, "kind", x))
def test_every_drive_message_goes_only_to_the_qq_groups_that_tick_it_without_the_cover(tmp_path, event, icon, text):
    fake = FakeQQ()
    send(notifier(tmp_path, fake, {**SUBSCRIBED, "kinds": [str(event.kind)]},
                  {**SUBSCRIBED, "openid": "G2", "kinds": ["ready", "uploaded"]}), event)

    (message,) = fake.texts("G1")
    assert message.startswith(f"{icon} **A** {text}\ntitle v1\n")
    assert "![" not in message and fake.uploads == 0
    assert fake.texts("G2") == []


def test_no_drive_message_is_in_a_new_groups_defaults_and_each_can_be_ticked(tmp_path, monkeypatch):
    group, _ = QQGroups(tmp_path / "g.json").register("G9")
    drive_kinds = {"drive_uploaded", "drive_failed", "drive_authorization_lapsed"}
    assert not drive_kinds & set(group["kinds"])
    kinds = app(tmp_path, monkeypatch).get("/api/notify").json()["kinds"]
    assert {"id": "drive_uploaded", "label": "网盘上传完成"} in kinds
    assert {"id": "drive_failed", "label": "网盘上传失败"} in kinds
    assert {"id": "drive_authorization_lapsed", "label": "网盘授权失效"} in kinds


def test_authorization_lapsed_is_a_warning_on_discord_and_drive_upload_failed_a_failure():
    assert discord(LAPSED)["embeds"][0]["color"] == 0xD97706
    assert discord(FAILED)["embeds"][0]["color"] == 0xDC2626
