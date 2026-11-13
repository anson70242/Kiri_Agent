"""A TwitCasting live goes through the real check, notifier, Discord and QQ text.

The site and Discord are fakes; the pages are the hand-made ones of the monitor tests.
"""

import asyncio
import json

import httpx

from backend.monitor.twitcasting import TwitCastingMonitor
from backend.notify import EventKind, LiveNotifier, StreamEvent
from backend.notify import qq
from backend.notify.discord import DiscordNotifier

from .test_twitcasting_monitor import AVATAR, TC, channel_page

TWITCASTING_BLUE = 0x3381FF  # the site's own theme colour


def test_live_and_end_reach_discord_and_qq_text():
    pages = [channel_page(onlive="false"), channel_page(), channel_page(onlive="false")]
    sent, events = [], []

    def discord(req):
        sent.append(json.loads(req.content))
        return httpx.Response(204)

    async def go():
        site = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=pages.pop(0))))
        hook = httpx.AsyncClient(transport=httpx.MockTransport(discord))
        notifier = LiveNotifier([DiscordNotifier("https://discord.test/hook", client=hook)], offline_grace=0.05)
        notifier.on_ended.append(events.append)
        monitor = TwitCastingMonitor(site)
        old = None
        for _ in range(3):
            new = await monitor.check(TC)
            await notifier.on_change(new, old)
            old = new
        await asyncio.sleep(0.2)  # past the grace period
        await notifier.aclose()
        await site.aclose()

    asyncio.run(go())

    went_live, ended = sent
    assert went_live["content"] == "🔴 **Mika** 开播了！"
    e = went_live["embeds"][0]
    assert e["title"] == "Night chat"
    assert e["url"] == "https://twitcasting.tv/mika_test01"
    assert e["author"] == {"name": "Mika · twitcasting", "url": "https://twitcasting.tv/mika_test01",
                           "icon_url": AVATAR}
    assert e["image"]["url"] == "https://imagegw03.example/image3/1/cover-1.jpg?__t=1&s=2"
    assert e["color"] == TWITCASTING_BLUE

    assert ended["content"] == "⚫ **Mika** 下播了"
    assert [f["name"] for f in ended["embeds"][0]["fields"]] == ["直播时长"]

    (end_event,) = events  # the automatic download hears of the ended live
    assert end_event.kind is EventKind.OFFLINE
    assert end_event.status.stream_id == "900000001"
    assert end_event.status.url == "https://twitcasting.tv/mika_test01"

    text = qq.message(StreamEvent(EventKind.LIVE, end_event.status))
    assert "Mika · twitcasting" in text and "Night chat" in text
    assert "https://twitcasting.tv/mika_test01" in text
