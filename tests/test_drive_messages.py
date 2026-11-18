"""The drive's messages: "☁️ 网盘上传完成" on Discord and to the QQ groups that tick it, and
"全部处理完成" carrying a drive link like any other link."""

from __future__ import annotations

import asyncio
import json

import httpx

from backend.notify import EventKind, Link, StreamEvent
from backend.notify.discord import DiscordNotifier
from backend.notify.qq import QQGroups

from .helpers import A, live
from .test_notify_api import app
from .test_qq import COVER, SUBSCRIBED, FakeQQ, notifier, send

DRIVE_LINK = "https://drive.example/s/abc?pwd=x1y2"
DRIVE_LABEL = "全部下载（夸克网盘：影片、弹幕、中日字幕 SRT、路灯表）"
PAGE = "https://kiri.example/archives#A%2F20261110%2F%E6%AD%8C%E6%9E%A0"
UPLOADED = StreamEvent(EventKind.DRIVE_UPLOADED, live(A, thumbnail=COVER),
                       detail="网盘路径：`录播/A/v.zip`\n没有网盘链接：分享被拒绝")
READY = StreamEvent(EventKind.READY, live(A), detail="影片 ✅ · 路灯表 ✅",
                    links=(Link(DRIVE_LABEL, DRIVE_LINK), Link("录播页面", PAGE)))


def discord(event: StreamEvent) -> dict:
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(204)

    n = DiscordNotifier("https://discord.test/hook", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(n.send(event))
    (payload,) = sent
    return payload


def test_drive_copy_uploaded_goes_to_discord():
    payload = discord(UPLOADED)
    assert payload["content"] == "☁️ **A** 网盘上传完成"
    assert "录播/A/v.zip" in payload["embeds"][0]["description"]
    assert "image" not in payload["embeds"][0]


def test_drive_copy_uploaded_goes_only_to_the_qq_groups_that_tick_it_without_the_cover(tmp_path):
    fake = FakeQQ()
    send(notifier(tmp_path, fake, {**SUBSCRIBED, "kinds": ["drive_uploaded"]},
                  {**SUBSCRIBED, "openid": "G2", "kinds": ["ready", "uploaded"]}), UPLOADED)

    (text,) = fake.texts("G1")
    assert text.startswith("☁️ **A** 网盘上传完成\ntitle v1\n") and "录播/A/v.zip" in text
    assert "![" not in text and fake.uploads == 0
    assert fake.texts("G2") == []


def test_a_new_qq_group_does_not_get_drive_copy_uploaded_until_it_ticks_it(tmp_path, monkeypatch):
    group, _ = QQGroups(tmp_path / "g.json").register("G9")
    assert "drive_uploaded" not in group["kinds"]
    kinds = app(tmp_path, monkeypatch).get("/api/notify").json()["kinds"]
    assert {"id": "drive_uploaded", "label": "网盘上传完成"} in kinds


def test_ready_shows_the_drive_link_like_any_link():
    assert discord(READY)["embeds"][0]["description"] == \
        f"影片 ✅ · 路灯表 ✅\n[{DRIVE_LABEL}]({DRIVE_LINK})\n[录播页面]({PAGE})"


def test_ready_shows_the_drive_link_like_any_link_on_qq(tmp_path):
    fake = FakeQQ(cover_ok=False)
    send(notifier(tmp_path, fake, {**SUBSCRIBED, "kinds": ["ready"]}), READY)
    (text,) = fake.texts("G1")
    assert text == ("✅ **A** 全部处理完成\ntitle v1\n影片 ✅ · 路灯表 ✅\n"
                    f"[{DRIVE_LABEL}]({DRIVE_LINK})\n[录播页面]({PAGE})\n切片小助手 自动下载")
