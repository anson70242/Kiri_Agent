"""YouTube / Twitch page parsing, including pages that must NOT read as "offline"."""

import asyncio
import json

import httpx
import pytest

from backend.monitor import LiveState, Streamer
from backend.monitor.base import ld_json_nodes
from backend.monitor.twitch import TwitchMonitor
from backend.monitor.youtube import YouTubeMonitor

YT = Streamer("Y", "youtube", "@y")
TW = Streamer("T", "twitch", "t")


# ---------- YouTube ----------

def yt_watch_page(*, is_live=False, is_upcoming=False, video_id="abc123", start="2026-11-10T08:00:50+00:00"):
    player = {
        "videoDetails": {"videoId": video_id, "title": "歌枠", "isLive": is_live, "isUpcoming": is_upcoming},
        "microformat": {"playerMicroformatRenderer": {"liveBroadcastDetails": {"isLiveNow": is_live, "startTimestamp": start}}},
    }
    return (
        f'<link rel="canonical" href="https://www.youtube.com/watch?v={video_id}">'
        f'<meta property="og:image" content="https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg?v=1&amp;x=2">'
        f"<script>var ytInitialPlayerResponse = {json.dumps(player)};var meta = 1;</script>"
        '"videoOwnerRenderer":{"thumbnail":{"thumbnails":[{"url":"https://yt3.ggpht.com/av=s48-c-k"}]}}'
    )


YT_CHANNEL_PAGE = (
    '<link rel="canonical" href="https://www.youtube.com/channel/UCxyz">'
    '<meta property="og:image" content="https://yt3.googleusercontent.com/av=s900-c-k">'
)


def test_youtube_live():
    st = YouTubeMonitor.parse_page(YT, yt_watch_page(is_live=True))
    assert st.state is LiveState.LIVE
    assert st.stream_id == "abc123"
    assert st.url == "https://www.youtube.com/watch?v=abc123"
    assert st.title == "歌枠"
    assert st.started_at.isoformat() == "2026-11-10T08:00:50+00:00"
    assert st.thumbnail == "https://i.ytimg.com/vi/abc123/maxresdefault.jpg?v=1&x=2"
    assert st.avatar == "https://yt3.ggpht.com/av=s176-c-k"


def test_youtube_upcoming():
    st = YouTubeMonitor.parse_page(YT, yt_watch_page(is_upcoming=True))
    assert st.state is LiveState.UPCOMING
    assert st.stream_id == "abc123"


def test_youtube_just_ended_watch_page_is_offline():
    st = YouTubeMonitor.parse_page(YT, yt_watch_page())
    assert st.state is LiveState.OFFLINE
    assert st.thumbnail is None


def test_youtube_channel_page_is_offline():
    st = YouTubeMonitor.parse_page(YT, YT_CHANNEL_PAGE)
    assert st.state is LiveState.OFFLINE
    assert st.avatar == "https://yt3.googleusercontent.com/av=s176-c-k"


@pytest.mark.parametrize("page", [
    "",  # empty response
    '<html><title>Before you continue to YouTube</title><form action="https://consent.youtube.com/save">',
    "<html>Sign in to confirm you’re not a bot</html>",
    '<link rel="canonical" href="https://www.google.com/sorry/index">',
])
def test_youtube_unrecognized_page_is_an_error_not_offline(page):
    with pytest.raises(ValueError):
        YouTubeMonitor.parse_page(YT, page)


def test_youtube_check_turns_errors_into_unknown():
    async def go():
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<html>bot check</html>"))
        async with httpx.AsyncClient(transport=transport) as client:
            st = await YouTubeMonitor(client).check(YT)
        assert st.state is LiveState.UNKNOWN
        assert "unrecognized" in st.error

    asyncio.run(go())


# The /streams tab lists a channel's streams as "lockup" items (trimmed from the real
# page). /live can resolve to a scheduled item even while a members-only stream is
# live (seen 2026-11-12 on @wakuse_ito), so a LIVE badge here must win.

def yt_lockup(video_id, title, *, live=False, members=False, upcoming=False):
    badge = (
        {"text": "LIVE", "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE"} if live
        else {"text": "Upcoming", "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_DEFAULT"} if upcoming
        else {"text": "2:31:32", "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_DEFAULT"}
    )
    rows = [{"badges": [{"badgeViewModel": {"badgeText": "Members only", "badgeStyle": "BADGE_MEMBERS_ONLY"}}]}] if members else []
    return {"richItemRenderer": {"content": {"lockupViewModel": {
        "contentId": video_id,
        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {
            "image": {"sources": [{"url": f"https://i.ytimg.com/vi/{video_id}/hq720.jpg?a=1&b=2", "width": 720}]},
            "overlays": [{"thumbnailBottomOverlayViewModel": {"badges": [{"thumbnailBadgeViewModel": badge}]}}],
        }},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": title},
            "metadata": {"contentMetadataViewModel": {"metadataRows": rows}},
        }},
    }}}}


def yt_streams_page(*items):
    data = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [
        {"tabRenderer": {"title": "Home"}},
        {"tabRenderer": {"title": "Live", "content": {"richGridRenderer": {"contents": list(items)}}}},
    ]}}}
    return (
        '<link rel="canonical" href="https://www.youtube.com/channel/UCxyz">'
        f"<script>var ytInitialData = {json.dumps(data, ensure_ascii=False)};</script>"
    )


MEMBERS_TITLE = "【 メン限 】11月のいとっこちゃん作戦会議【 惑世いと┆#vtuber 】"
WAKUSE_STREAMS = yt_streams_page(
    yt_lockup("DhreCl4ocKc", "weekly schedule", upcoming=True),
    yt_lockup("wDoutWmF4uY", "【💿News! 】とっておきの3つめのCDが販売されました♪", upcoming=True),
    yt_lockup("DggkYPYfAtk", MEMBERS_TITLE, live=True, members=True),
    yt_lockup("yKlo9BkZVrg", "【 歌枠 】寒くなってきたねえ"),
)


def test_youtube_streams_tab_finds_members_only_live_among_upcoming():
    st = YouTubeMonitor.parse_streams_page(YT, WAKUSE_STREAMS)
    assert st.state is LiveState.LIVE
    assert st.stream_id == "DggkYPYfAtk"
    assert st.title == MEMBERS_TITLE
    assert st.url == "https://www.youtube.com/watch?v=DggkYPYfAtk"
    assert st.thumbnail == "https://i.ytimg.com/vi/DggkYPYfAtk/hq720.jpg?a=1&b=2"


def test_youtube_streams_tab_without_live_finds_nothing():
    page = yt_streams_page(yt_lockup("u1", "schedule", upcoming=True), yt_lockup("v1", "archive"))
    assert YouTubeMonitor.parse_streams_page(YT, page) is None


def test_youtube_streams_tab_unrecognized_is_an_error():
    with pytest.raises(ValueError):
        YouTubeMonitor.parse_streams_page(YT, "<html>Sign in to confirm you’re not a bot</html>")


def check_youtube(live_page, streams):
    """Run YouTubeMonitor.check with /live and /streams served by the given fakes."""
    def handler(req):
        if req.url.path.endswith("/live"):
            return httpx.Response(200, text=live_page)
        if req.url.path.endswith("/streams"):
            return streams if isinstance(streams, httpx.Response) else httpx.Response(200, text=streams)
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await YouTubeMonitor(client).check(YT)

    return asyncio.run(go())


def test_youtube_members_only_live_wins_over_upcoming_on_live_tab():
    # /live serves the far-future premiere; the members-only stream is live meanwhile.
    st = check_youtube(yt_watch_page(is_upcoming=True, video_id="wDoutWmF4uY"), WAKUSE_STREAMS)
    assert st.state is LiveState.LIVE
    assert st.stream_id == "DggkYPYfAtk"
    assert st.title == MEMBERS_TITLE
    assert st.thumbnail == "https://i.ytimg.com/vi/DggkYPYfAtk/hq720.jpg?a=1&b=2"
    assert st.avatar == "https://yt3.ggpht.com/av=s176-c-k"


def test_youtube_members_only_live_when_live_tab_shows_channel_page():
    st = check_youtube(YT_CHANNEL_PAGE, WAKUSE_STREAMS)
    assert st.state is LiveState.LIVE
    assert st.stream_id == "DggkYPYfAtk"
    assert st.avatar == "https://yt3.googleusercontent.com/av=s176-c-k"


def test_youtube_upcoming_stays_upcoming_when_streams_tab_has_no_live():
    page = yt_streams_page(yt_lockup("abc123", "歌枠", upcoming=True))
    st = check_youtube(yt_watch_page(is_upcoming=True), page)
    assert st.state is LiveState.UPCOMING
    assert st.stream_id == "abc123"


def test_youtube_public_live_does_not_need_streams_tab():
    st = check_youtube(yt_watch_page(is_live=True), httpx.Response(503))
    assert st.state is LiveState.LIVE
    assert st.stream_id == "abc123"


@pytest.mark.parametrize("streams", [httpx.Response(503), "<html>bot check</html>"])
def test_youtube_failed_streams_tab_is_unknown_not_offline(streams):
    st = check_youtube(YT_CHANNEL_PAGE, streams)
    assert st.state is LiveState.UNKNOWN


@pytest.mark.parametrize("channel,url", [
    ("@foo", "https://www.youtube.com/@foo/live"),
    ("foo", "https://www.youtube.com/@foo/live"),
    ("UCabc", "https://www.youtube.com/channel/UCabc/live"),
    ("https://www.youtube.com/@foo", "https://www.youtube.com/@foo/live"),
    ("https://www.youtube.com/@foo/live/", "https://www.youtube.com/@foo/live"),
])
def test_youtube_live_url(channel, url):
    assert YouTubeMonitor.live_url(channel) == url


# ---------- Twitch ----------

def tw_page(live: bool, profile: bool = True, start="2026-11-09T19:49:26Z"):
    graph = []
    if profile:
        graph.append({"@type": "ProfilePage", "mainEntity": {"@type": "Person", "image": "https://cdn/avatar.png"}})
    if live:
        graph.append({
            "@type": "VideoObject", "description": "GTA 🎮",
            "thumbnailUrl": ["https://cdn/p-80x45.jpg", "https://cdn/p-640x360.jpg"],
            "publication": {"@type": "BroadcastEvent", "startDate": start, "isLiveBroadcast": True},
        })
    og = '<meta property="og:description" content="GTA | Streaming GTA for 47,923 viewers.">' if live else ""
    return f'{og}<script type="application/ld+json">{json.dumps({"@graph": graph})}</script>'


def test_twitch_live():
    nodes = ld_json_nodes(tw_page(True))
    st = TwitchMonitor.parse_page(TW, "https://www.twitch.tv/t", nodes, "GTA | Streaming GTA for 47,923 viewers.")
    assert st.state is LiveState.LIVE
    assert st.title == "GTA 🎮"
    assert st.stream_id == "2026-11-09T19:49:26Z"
    assert st.viewers == 47923
    assert st.thumbnail.startswith("https://cdn/p-640x360.jpg?t=")
    assert st.avatar == "https://cdn/avatar.png"


def twitch_check(*pages):
    """Run TwitchMonitor.check against a fake server returning `pages` in order."""
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(200, text=pages[min(len(calls), len(pages)) - 1])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TwitchMonitor(client).check(TW)

    return asyncio.run(go()), len(calls)


def test_twitch_live_and_offline_via_check():
    assert twitch_check(tw_page(True))[0].state is LiveState.LIVE
    assert twitch_check(tw_page(False))[0].state is LiveState.OFFLINE


def test_twitch_generic_page_is_retried_once():
    st, calls = twitch_check(tw_page(True, profile=False), tw_page(True))
    assert st.state is LiveState.LIVE
    assert calls == 2


def test_twitch_generic_page_twice_is_unknown_not_offline():
    st, calls = twitch_check(tw_page(False, profile=False))
    assert st.state is LiveState.UNKNOWN
    assert calls == 2


@pytest.mark.parametrize("channel", ["T", "@t", "https://www.twitch.tv/t", "https://www.twitch.tv/t/videos?x=1"])
def test_twitch_login(channel):
    assert TwitchMonitor.login(channel) == "t"


def test_http_error_is_unknown():
    async def go():
        transport = httpx.MockTransport(lambda req: httpx.Response(503))
        async with httpx.AsyncClient(transport=transport) as client:
            return await TwitchMonitor(client).check(TW), await YouTubeMonitor(client).check(YT)

    for st in asyncio.run(go()):
        assert st.state is LiveState.UNKNOWN
