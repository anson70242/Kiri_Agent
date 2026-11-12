"""Discord webhook: message content and HTTP failure handling."""

import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from backend.notify import EventKind, LiveNotifier, PermanentNotifyError, StreamEvent
from backend.notify.discord import DiscordNotifier

from .helpers import T0, B, live, off


def fake_discord(*responses):
    """Discord stand-in returning `responses` in order (last one repeats)."""
    sent = []

    def handler(req: httpx.Request):
        sent.append(json.loads(req.content))
        r = responses[min(len(sent), len(responses)) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DiscordNotifier("https://discord.test/hook", mention="@here", client=client), sent


def send(notifier, event):
    async def go():
        try:
            await notifier.send(event)
        finally:
            await notifier.aclose()

    asyncio.run(go())


LIVE_EVENT = StreamEvent(EventKind.LIVE, live(thumbnail="https://cdn/cover.jpg", avatar="https://cdn/a.png"))
OFFLINE_EVENT = StreamEvent(EventKind.OFFLINE, live(B, thumbnail="https://cdn/cover.jpg"), T0 + timedelta(hours=2, minutes=5))


def test_live_message():
    n, sent = fake_discord(httpx.Response(204))
    send(n, LIVE_EVENT)
    msg = sent[0]
    assert msg["content"] == "@here 🔴 **A** 开播了！"
    assert msg["allowed_mentions"]["parse"]  # ping allowed on go-live
    e = msg["embeds"][0]
    assert e["title"] == "title v1" and e["url"] == "https://x/v1"
    assert e["image"]["url"] == "https://cdn/cover.jpg"
    assert e["author"]["icon_url"] == "https://cdn/a.png"
    assert e["color"] == 0xFF0000


def test_offline_message_has_duration_and_no_ping():
    n, sent = fake_discord(httpx.Response(204))
    send(n, OFFLINE_EVENT)
    msg = sent[0]
    assert msg["content"] == "⚫ **B** 下播了"
    assert msg["allowed_mentions"] == {"parse": []}
    e = msg["embeds"][0]
    assert e["fields"] == [{"name": "直播时长", "value": "2 小时 5 分", "inline": True}]
    assert e["thumbnail"]["url"] == "https://cdn/cover.jpg"
    assert "image" not in e


def test_long_title_is_truncated():
    n, sent = fake_discord(httpx.Response(204))
    s = live()
    s.title = "x" * 1000
    send(n, StreamEvent(EventKind.LIVE, s))
    assert len(sent[0]["embeds"][0]["title"]) == 256


def test_rate_limit_waits_and_retries():
    n, sent = fake_discord(httpx.Response(429, json={"retry_after": 0.05}), httpx.Response(204))
    send(n, LIVE_EVENT)
    assert len(sent) == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_rejected_webhook_is_permanent(code):
    n, _ = fake_discord(httpx.Response(code, text="Unknown Webhook"))
    with pytest.raises(PermanentNotifyError):
        send(n, LIVE_EVENT)


def test_server_error_is_retryable():
    n, _ = fake_discord(httpx.Response(502))
    with pytest.raises(httpx.HTTPStatusError):
        send(n, LIVE_EVENT)


def test_through_live_notifier_survives_outage():
    """Discord down for a while (5xx, connection errors), then recovers: delivered once."""

    async def go():
        n, sent = fake_discord(
            httpx.Response(500),
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("timed out"),
            httpx.Response(204),
        )
        notifier = LiveNotifier([n], offline_grace=0, retry_base_delay=0.01)
        await notifier.on_change(off(), None)
        await notifier.on_change(live(), off())
        await notifier.drain()
        await notifier.aclose()
        return sent

    sent = asyncio.run(go())
    assert len(sent) == 4


@pytest.mark.parametrize("kind, footer", [
    (EventKind.LIVE, "切片小助手 直播通知"),
    (EventKind.OFFLINE, "切片小助手 直播通知"),
    (EventKind.DOWNLOADED, "切片小助手 自动下载"),
    (EventKind.TRANSCRIBE_FAILED, "切片小助手 自动下载"),
    (EventKind.ANALYZED, "切片小助手 自动路灯"),
    (EventKind.UPLOADED, "切片小助手 自动路灯"),
    (EventKind.UPLOAD_FAILED, "切片小助手 自动路灯"),
    (EventKind.READY, "切片小助手 自动下载"),
])
def test_footer_says_what_sent_it(kind, footer):
    n, sent = fake_discord(httpx.Response(204))
    send(n, StreamEvent(kind, live(), T0 + timedelta(hours=1), detail="x"))
    assert sent[0]["embeds"][0]["footer"]["text"] == footer


def test_alert_is_a_plain_message():
    n, sent = fake_discord(httpx.Response(204))

    async def go():
        await n.alert("QQ 机器人已离线 12 分钟")
        await n.aclose()

    asyncio.run(go())
    assert sent[0]["content"] == "⚠️ QQ 机器人已离线 12 分钟"
    assert sent[0]["allowed_mentions"] == {"parse": []}
