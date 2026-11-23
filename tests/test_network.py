"""Real YouTube / Twitch / TwitCasting checks, and the metadata of a TwitCasting replay when
the TwitCasting cookies file is present. Run with: uv run pytest -m network"""

import asyncio
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from backend.downloader import downloader_for, load_settings
from backend.monitor import LiveState, StreamMonitor, Streamer
from backend.monitor.base import make_client
from backend.monitor.twitcasting import TwitCastingMonitor

pytestmark = pytest.mark.network

CONFIG = Path(__file__).resolve().parents[1] / "config.json"
EXTRA = [
    Streamer("Lofi Girl", "youtube", "@LofiGirl"),  # 24/7 live
    Streamer("YouTube", "youtube", "@YouTube"),  # normally offline
    Streamer("lirik", "twitch", "lirik"),
]


def streamers():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))["monitor"]["streamers"]
    return [Streamer(s["name"], s["platform"], s["channel"]) for s in cfg] + EXTRA


def test_repeated_real_checks_are_stable():
    """Poll every streamer several times: no failures, and no state flip-flopping."""
    rounds = 5

    async def go():
        seen: dict[str, list] = {}
        async with StreamMonitor(streamers()) as mon:
            for _ in range(rounds):
                for st in await mon.poll_once():
                    seen.setdefault(st.streamer.id, []).append(st)
                await asyncio.sleep(3)
        return seen

    seen = asyncio.run(go())
    for sid, results in seen.items():
        states = Counter(r.state for r in results)
        errors = [r.error for r in results if r.error]
        print(sid, dict(states), errors[:1])
        assert LiveState.UNKNOWN not in states, f"{sid}: check failed: {errors}"
        assert len(states) == 1, f"{sid}: state flip-flopped: {states}"
        live = [r for r in results if r.is_live]
        assert len({r.stream_id for r in live}) <= 1, f"{sid}: stream id changed: {[r.stream_id for r in live]}"
        for r in live:
            assert r.stream_id and r.title and r.url and r.started_at, f"{sid}: incomplete live status {r}"


def test_known_live_channel_is_live():
    async def go():
        async with StreamMonitor([EXTRA[0]]) as mon:
            return (await mon.poll_once())[0]

    st = asyncio.run(go())
    assert st.state is LiveState.LIVE, st


# ---------- TwitCasting ----------
# Which channel is live changes all the time: the site's front page lists live
# channels (`data-userid`), and the stream server says whether one is live now.

TWITCASTING_MAYBE_OFFLINE = ["twitcasting_jp", "twitcasting_info"]  # the site's own accounts


async def twitcasting_movie(client, channel):
    """(movie id, live?) from the stream server, or None for an unknown channel."""
    r = await client.get("https://twitcasting.tv/streamserver.php",
                         params={"target": channel, "mode": "client", "player": "pc_web"})
    r.raise_for_status()
    movie = r.json().get("movie")
    return (str(movie["id"]), movie["live"]) if movie else None


def test_twitcasting_live_channel_is_live():
    async def go():
        async with make_client() as client:
            front = (await client.get("https://twitcasting.tv/")).text
            channels = list(dict.fromkeys(re.findall(r'data-userid="([^"]+)"', front)))
            assert channels, "no live channel on the TwitCasting front page"
            tried = []
            for channel in channels[:10]:
                movie = await twitcasting_movie(client, channel)
                if not (movie and movie[1]):
                    continue
                st = await TwitCastingMonitor(client).check(Streamer(channel, "twitcasting", channel))
                tried.append((channel, st.state, st.error))
                if st.is_live:
                    return channel, movie[0], st
            pytest.fail(f"no live TwitCasting channel confirmed: {tried}")

    channel, movie_id, st = asyncio.run(go())
    print(channel, st)
    assert st.stream_id == movie_id
    assert st.url == f"https://twitcasting.tv/{channel}"
    assert st.title and st.started_at and st.avatar and st.thumbnail


def test_twitcasting_offline_channel_is_offline():
    async def go():
        async with make_client() as client:
            for channel in TWITCASTING_MAYBE_OFFLINE:
                movie = await twitcasting_movie(client, channel)
                if movie and not movie[1]:
                    return await TwitCastingMonitor(client).check(Streamer(channel, "twitcasting", channel))
            pytest.skip(f"every channel of {TWITCASTING_MAYBE_OFFLINE} is live or unknown right now")

    st = asyncio.run(go())
    print(st)
    assert st.state is LiveState.OFFLINE, st
    assert st.avatar


PUBLISHED_REPLAY = "https://twitcasting.tv/shino_nome22/movie/841887581"  # published on 2026-11-13


def test_twitcasting_published_replay_metadata_with_the_cookies_file():
    """Reads (never downloads) a published replay with the owner's login. Skipped without
    the cookies file (CI has none). yt-dlp writes refreshed cookies back to the file."""
    settings, streamers = load_settings(CONFIG)
    cookies = settings.twitcasting_cookies_file
    if not (cookies and cookies.is_file()):
        pytest.skip(f"no TwitCasting cookies file at {cookies}")
    info = downloader_for(PUBLISHED_REPLAY, settings, streamers).info(PUBLISHED_REPLAY)
    print(info)
    assert (info.platform, info.id, info.channel) == ("twitcasting", "841887581", "shino_nome22")
    assert re.fullmatch(r".+ \d{6}", info.title) and " / " not in info.title
    assert info.duration and info.duration > 60
