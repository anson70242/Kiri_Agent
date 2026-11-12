"""QQ group notifications through the official QQ bot, against a fake QQ API (httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta

import httpx
import pytest

from backend.notify import EventKind, StreamEvent
from backend.notify.qq import QQGroups, QQNotifier, QQSettings

from .helpers import A, B, T0, live

SECRET = "app-SECRET-xyz"
COVER = "https://i.ytimg.com/vi/v1/hqdefault.jpg"
# A 1x1 JPEG is enough: only its bytes and its size header are used.
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c2024"
    "2e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000"
    "000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c115"
    "52d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a8384"
    "85868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1"
    "f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9")


class FakeQQ:
    """Records what the bot sends; answers like bots.qq.com / api.sgroup.qq.com."""

    def __init__(self, cover_ok=True, library_full=False):
        self.messages: list[tuple[str, dict]] = []  # (group_openid, body)
        self.cover_ok = cover_ok
        self.library = [{"image_id": "old-kiri", "file_name": "kiri-cover-1.jpg", "created_at": 1},
                        {"image_id": "theirs", "file_name": "other.jpg", "created_at": 0}] if library_full else []
        self.full = library_full
        self.deleted: list[str] = []
        self.uploads = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        url, path = str(request.url), request.url.path
        if url.startswith(COVER):
            return httpx.Response(200, content=JPEG) if self.cover_ok else httpx.Response(404)
        if path == "/app/getAppAccessToken":
            assert json.loads(request.content)["clientSecret"] == SECRET
            return httpx.Response(200, json={"access_token": "TOKEN1", "expires_in": "7200"})
        assert request.headers.get("Authorization") in ("QQBot TOKEN1", None) or "cos" in url
        if path == "/v2/media-images/prepare":
            if self.full:
                return httpx.Response(400, json={"code": 40035007, "message": "素材数量超限"})
            return httpx.Response(200, json={"upload_id": "U1", "presigned_url": "https://cos.example/put/U1"})
        if url.startswith("https://cos.example/put/"):
            self.uploads += 1
            return httpx.Response(200, headers={"ETag": '"abc"'})
        if path == "/v2/media-images/complete":
            return httpx.Response(200, json={"image_id": "I1", "url": "qqbot-media://image/v1/COVER1", "ttl": 7776000})
        if path == "/v2/media-images" and request.method == "GET":
            return httpx.Response(200, json={"images": self.library, "next_cursor": ""})
        if m := re.fullmatch(r"/v2/media-images/(.+)/delete", path):
            self.deleted.append(m.group(1))
            self.full = False
            return httpx.Response(200, json={})
        if m := re.fullmatch(r"/v2/groups/(.+)/messages", path):
            self.messages.append((m.group(1), json.loads(request.content)))
            return httpx.Response(200, json={"id": f"M{len(self.messages)}"})
        raise AssertionError(f"unexpected {request.method} {url}")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def texts(self, group: str) -> list[str]:
        return [(b.get("markdown") or {}).get("content") or b.get("content") for g, b in self.messages if g == group]


def groups(tmp_path, *entries: dict) -> QQGroups:
    g = QQGroups(tmp_path / "qq_groups.json")
    for e in entries:
        g.register(e["openid"])
        g.update(e["openid"], **{k: v for k, v in e.items() if k != "openid"})
    return g


def notifier(tmp_path, fake, *entries) -> QQNotifier:
    return QQNotifier(QQSettings("APP1", SECRET), groups(tmp_path, *entries), client=fake.client(), gap=0)


def send(n: QQNotifier, event: StreamEvent) -> None:
    asyncio.run(n.send(event))


SUBSCRIBED = {"openid": "G1", "name": "章鱼烧", "enabled": True, "streamers": [A.id],
              "kinds": ["live", "offline", "downloaded", "uploaded"]}


def test_live_goes_to_subscribed_groups_with_the_cover_on_the_second_line(tmp_path):
    fake = FakeQQ()
    n = notifier(tmp_path, fake, SUBSCRIBED,
                 {**SUBSCRIBED, "openid": "G2", "streamers": [B.id]},  # another streamer
                 {**SUBSCRIBED, "openid": "G3", "enabled": False},  # switched off
                 {**SUBSCRIBED, "openid": "G4", "kinds": ["offline"]})  # not this kind
    status = live(A, viewers=1234, thumbnail=COVER)
    send(n, StreamEvent(EventKind.LIVE, status))

    assert [g for g, _ in fake.messages] == ["G1"]
    (_, body), = fake.messages
    assert body["msg_type"] == 2
    lines = body["markdown"]["content"].split("\n")
    assert lines[0] == "🔴 **A** 开播了！"
    assert lines[1].startswith("![") and lines[1].endswith("(qqbot-media://image/v1/COVER1)")
    assert "A · youtube" in lines and "title v1" in lines
    assert any(line.startswith("👀 1,234 人观看 · 开始于 ") for line in lines)  # local time (UTC+8)
    assert "开始于 16:00" in body["markdown"]["content"]  # T0 is 08:00 UTC
    assert "[https://x/v1](https://x/v1)" in lines
    assert lines[-1] == "切片小助手 开播通知"
    assert fake.uploads == 1


def test_without_a_cover_the_message_is_text_only(tmp_path):
    fake = FakeQQ(cover_ok=False)
    send(notifier(tmp_path, fake, SUBSCRIBED), StreamEvent(EventKind.LIVE, live(A, thumbnail=COVER)))
    (text,) = fake.texts("G1")
    assert text.startswith("🔴 **A** 开播了！\nA · youtube\n") and "![" not in text


def test_a_full_media_library_drops_our_oldest_cover(tmp_path):
    fake = FakeQQ(library_full=True)
    send(notifier(tmp_path, fake, SUBSCRIBED), StreamEvent(EventKind.LIVE, live(A, thumbnail=COVER)))
    assert fake.deleted == ["old-kiri"]  # never images someone else uploaded
    assert "qqbot-media://image/v1/COVER1" in fake.texts("G1")[0]


def test_ended_and_pipeline_messages(tmp_path):
    fake = FakeQQ()
    n = notifier(tmp_path, fake, {**SUBSCRIBED, "kinds": ["offline", "uploaded", "analyzed"]})
    status = live(A)
    send(n, StreamEvent(EventKind.OFFLINE, status, ended_at=T0 + timedelta(hours=3, minutes=20)))
    send(n, StreamEvent(EventKind.UPLOADED, status, detail="https://docs.qq.com/sheet/S1?tab=t1"))
    send(n, StreamEvent(EventKind.ANALYZED, status, detail="00:30:00 ★9 名场面"))
    send(n, StreamEvent(EventKind.DOWNLOADED, status, detail="6.2 GB"))  # not subscribed

    off, up, an = fake.texts("G1")
    assert off == "⚫ **A** 下播了\ntitle v1\n⏱ 直播时长 3 小时 20 分\n切片小助手 开播通知"
    assert up == ("📤 **A** 路灯表已上传腾讯文档\ntitle v1\n"
                  "[https://docs.qq.com/sheet/S1?tab=t1](https://docs.qq.com/sheet/S1?tab=t1)\n切片小助手 自动路灯")
    assert an.endswith("00:30:00 ★9 名场面\n切片小助手 自动路灯")


def test_markdown_in_titles_is_escaped(tmp_path):
    fake = FakeQQ()
    status = live(A)
    status.title = "【歌枠】*sing* [all] _night_"
    send(notifier(tmp_path, fake, SUBSCRIBED), StreamEvent(EventKind.OFFLINE, status))
    assert r"【歌枠】\*sing\* \[all\] \_night\_" in fake.texts("G1")[0]


def test_a_failing_group_does_not_stop_the_others(tmp_path):
    fake = FakeQQ()
    real = fake.handle

    def flaky(request):
        if request.url.path == "/v2/groups/G1/messages":
            return httpx.Response(400, json={"code": 40034105, "message": "群主未开启主动消息"})
        return real(request)

    fake.handle = flaky
    n = notifier(tmp_path, fake, SUBSCRIBED, {**SUBSCRIBED, "openid": "G2"})
    send(n, StreamEvent(EventKind.OFFLINE, live(A)))  # must not raise
    assert [g for g, _ in fake.messages] == ["G2"]
    (entry,) = [e for e in n.history.entries() if e["target"] == "G1"]
    assert not entry["ok"] and "40034105" in entry["error"]


def test_the_secret_never_shows(tmp_path):
    assert SECRET not in repr(QQSettings("APP1", SECRET))


# ---- wiring: load_notifier, history, alerts ----

def write_notify_config(tmp_path, qq: dict | None = None):
    cfg = tmp_path / "config.json"
    notify = {"discord": {"webhook_env": "KIRI_T_HOOK"}, "state_file": str(tmp_path / "state.json"),
              "log_file": str(tmp_path / "notify_log.json")}
    if qq is not None:
        notify["qq"] = {"app_id_env": "KIRI_T_QQ_ID", "secret_env": "KIRI_T_QQ_SECRET",
                        "groups_file": str(tmp_path / "qq_groups.json"), **qq}
    cfg.write_text(json.dumps({"notify": notify}), encoding="utf-8")
    return cfg


def test_qq_joins_only_with_credentials(tmp_path, monkeypatch):
    from backend.notify import load_notifier

    monkeypatch.setenv("KIRI_T_HOOK", "https://discord.test/hook")
    cfg = write_notify_config(tmp_path, qq={})
    assert [n.name for n in load_notifier(cfg).notifiers] == ["discord"]  # no QQ credentials yet

    monkeypatch.setenv("KIRI_T_QQ_ID", "APP1")
    monkeypatch.setenv("KIRI_T_QQ_SECRET", SECRET)
    notifier = load_notifier(cfg)
    assert sorted(n.name for n in notifier.notifiers) == ["discord", "qq"]
    assert notifier.qq is not None and notifier.qq.groups.path == tmp_path / "qq_groups.json"


def test_deliveries_are_logged_and_alerts_go_to_discord(tmp_path):
    from backend.notify import LiveNotifier
    from backend.notify.history import NotifyHistory

    from .helpers import Recorder

    rec = Recorder()
    alerts = []

    async def alert(text):
        alerts.append(text)

    rec.alert = alert
    rec.name = "discord"
    history = NotifyHistory(tmp_path / "log.json")
    ln = LiveNotifier([rec], history=history)

    async def go():
        await ln.notify(StreamEvent(EventKind.LIVE, live(A)))
        ln.alert("QQ 机器人已离线 10 分钟")
        await asyncio.sleep(0)
        await ln.aclose()

    asyncio.run(go())
    assert alerts == ["QQ 机器人已离线 10 分钟"]
    (alert_entry, live_entry) = NotifyHistory(tmp_path / "log.json").entries()  # newest first, kept on disk
    assert live_entry["channel"] == "discord" and live_entry["ok"] and "A" in live_entry["summary"]
    assert alert_entry["target"] == "提醒"
