"""TwitCasting channel check, against a fake site serving hand-made channel pages.

The pages keep only the markers verified on the real site (2026-11-13): the body's
`data-is-onlive`, `data-movie-id`, `data-started-at`, `og:title`, `og:image` and
`data-broadcaster-profile-image`. Names and titles are invented (the repository is public).
"""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from backend.monitor import LiveState, Streamer
from backend.monitor.twitcasting import TwitCastingMonitor

TC = Streamer("Mika", "twitcasting", "mika_test01")

STARTED_MS = 1794549666000  # 2026-11-13 06:01:06 UTC
AVATAR = "https://imagegw02.example/profile_images/1/av_bigger.jpg"
COVER = "https://imagegw03.example/image3/1/cover-1.jpg?__t=1&amp;s=2"


def channel_page(
    *,
    onlive: str | None = "true",
    movie_id: str = "900000001",
    og_title: str = "Night chat / Mika's room",
    started_ms: int = STARTED_MS,
) -> str:
    """A trimmed channel page. `onlive=None` leaves out the live marker."""
    marker = f'        data-is-onlive="{onlive}"\n' if onlive is not None else ""
    live_type = "live" if onlive == "true" else "movie"
    return (
        "<html lang=\"en\"><head>\n"
        f'        <meta property="og:image" content="{COVER}"/>\n'
        f'        <meta property="og:title" content="{og_title}"/>\n'
        '        <meta property="og:description" content="good morning!"/>\n'
        "</head>\n<body\n"
        '        data-is-mobile="false"\n'
        f"{marker}"
        '        data-view-mode="default"\n>\n'
        '<span id="updatetimer" class="tw-player-duration-time"\n'
        f'        data-live-type="{live_type}"\n'
        f'        data-started-at="{started_ms}"\n'
        '        data-duration="7745000">00:00</span>\n'
        '<div data-base-url="https://twitcasting.tv/" data-broadcaster-id="mika_test01"'
        ' data-broadcaster-name="Mika" data-broadcaster-screen-name="mika_test01"'
        f' data-broadcaster-profile-image="{AVATAR}" data-movie-id="{movie_id}"'
        ' data-account-type="not_logged_in"></div>\n'
        "</body></html>"
    )


def check(streamer, *responses):
    """Run TwitCastingMonitor.check against a fake site; return (status, requested urls)."""
    seen = []

    def handler(req):
        seen.append(str(req.url))
        r = responses[min(len(seen), len(responses)) - 1]
        return r if isinstance(r, httpx.Response) else httpx.Response(200, text=r)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TwitCastingMonitor(client).check(streamer)

    return asyncio.run(go()), seen


def test_live_channel_page_is_a_live_status():
    st, seen = check(TC, channel_page())

    assert seen == ["https://twitcasting.tv/mika_test01"]
    assert st.state is LiveState.LIVE
    assert st.stream_id == "900000001"
    assert st.url == "https://twitcasting.tv/mika_test01"
    assert st.title == "Night chat"
    assert st.started_at == datetime(2026, 11, 13, 6, 1, 6, tzinfo=timezone.utc)
    assert st.thumbnail == "https://imagegw03.example/image3/1/cover-1.jpg?__t=1&s=2"
    assert st.avatar == AVATAR
    assert st.error is None


def test_offline_channel_page_is_offline_with_the_avatar():
    st, _ = check(TC, channel_page(onlive="false", movie_id="899999999"))

    assert st.state is LiveState.OFFLINE
    assert st.avatar == AVATAR
    assert st.url == "https://twitcasting.tv/mika_test01"
    # The latest movie and the fake start time of an offline page are not a live.
    assert (st.stream_id, st.started_at, st.title, st.thumbnail) == (None, None, None, None)


FRONT_PAGE = (  # what an unknown channel id answers, with HTTP 200
    '<html><head><meta property="og:image" content="https://twitcasting.tv/img/top.png"/></head>'
    '<body data-is-mobile="false" data-header-information-level="low"></body></html>'
)


@pytest.mark.parametrize("page", [
    FRONT_PAGE,
    "",
    channel_page(onlive=None),  # the site moved the marker
    channel_page(onlive="maybe"),
])
def test_page_without_the_live_marker_is_unknown_not_offline(page):
    st, _ = check(TC, page)

    assert st.state is LiveState.UNKNOWN
    assert "live marker" in st.error


@pytest.mark.parametrize("code", [404, 500, 503])
def test_http_error_is_unknown_with_a_reason(code):
    st, _ = check(TC, httpx.Response(code))

    assert st.state is LiveState.UNKNOWN
    assert str(code) in st.error


@pytest.mark.parametrize("og_title,title", [
    ("Night chat / Mika's room", "Night chat"),
    ("Mika's room", "Mika's room"),  # the live has no title of its own
    ("Q&amp;A / part 2 / Mika's room", "Q&A / part 2"),  # only the last " / " is the channel's
    ("ウン億年ぶりのカラオケｪ / みかの部屋🍀", "ウン億年ぶりのカラオケｪ"),
    ("lofi/chill / Mika's room", "lofi/chill"),
])
def test_title_drops_the_channel_title(og_title, title):
    st, _ = check(TC, channel_page(og_title=og_title))

    assert st.title == title


@pytest.mark.parametrize("channel,requested", [
    ("mika_test01", "https://twitcasting.tv/mika_test01"),
    ("@mika_test01", "https://twitcasting.tv/mika_test01"),
    (" mika_test01 ", "https://twitcasting.tv/mika_test01"),
    ("https://twitcasting.tv/mika_test01", "https://twitcasting.tv/mika_test01"),
    ("twitcasting.tv/mika_test01/", "https://twitcasting.tv/mika_test01"),
    ("https://twitcasting.tv/mika_test01/movie/900000001", "https://twitcasting.tv/mika_test01"),
    ("https://twitcasting.tv/mika_test01?ref=share", "https://twitcasting.tv/mika_test01"),
    ("c:mika", "https://twitcasting.tv/c:mika"),
    ("@g:123456", "https://twitcasting.tv/g:123456"),
    ("https://twitcasting.tv/c:mika", "https://twitcasting.tv/c:mika"),
])
def test_channel_field_spellings(channel, requested):
    st, seen = check(Streamer("Mika", "twitcasting", channel), channel_page())

    assert seen == [requested]
    assert st.url == requested  # the watch address is the channel address
