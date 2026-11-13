"""A fake TwitCasting for downloader tests: hand-made replay pages, a fake site and a fake
yt-dlp, and the `tc` fixture that wires them up.

The replay pages keep only the markers verified on the real site (2026-11-13): the
`data-movie-playlist` attribute of a published replay (seen only when logged in), the
`data-account-type` of the visitor, the player's empty message, a password form, the
player's `<time datetime>` and `og:title`. Names, titles and cookie values are invented
(the repository is public).
"""

from __future__ import annotations

from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend.downloader import DownloadSettings
from backend.downloader.twitcasting import TwitCastingDownloader
from backend.monitor import Streamer

CHANNEL = "mika_test01"
MOVIE = "900000001"
REPLAY = f"https://twitcasting.tv/{CHANNEL}/movie/{MOVIE}"
MIKA = Streamer("Mika", "twitcasting", CHANNEL)
# A start time in the machine's own clock, the one the date folder uses: 2026-10-06 01:30:44.
START = datetime(2026, 10, 6, 1, 30, 44).astimezone()
COOKIE = "tc_ss=invented-session-value"


def replay_page(state: str = "published", *, og_title: str = "Night chat / Mika's room",
                start: datetime = START, message: str = "") -> str:
    """A trimmed replay page in one of the states the site shows; "other" is a page without
    the visitor's account type, which no verified state looks like."""
    account = {"login": 'data-account-type="not_logged_in"', "other": ""}.get(state, 'data-account-type=""')
    if state == "published":
        player = (f'<video id="player" data-movie-playlist=\'{{"2":[{{"startTime":0,"duration":2383000,'
                  f'"source":{{"url":"https:\\/\\/dl.example\\/{MOVIE}\\/master.m3u8"}}}}]}}\''
                  ' data-lang="en"></video>')
    elif state == "password":
        player = ('<form method="POST"><input type="password" name="password" value="">'
                  '<input type="submit" value="Watch"></form>')
    else:
        text = message or {"login": "Login required to watch",
                           "unpublished": "This video is not published"}.get(state, "")
        player = f'<div class="tw-player-empty-message">{text}</div>'
    return (
        '<html lang="en"><head>\n'
        f'<meta property="og:title" content="{og_title}"/>\n'
        "</head><body>\n"
        '<div class="tw-player-wrapper"><div class="tw-player partially-dark">\n'
        f"{player}\n</div></div>\n"
        '<span class="tw-player-meta__status_item">\n'
        '   <time data-kind="relative" data-locale="en" data-toggle="true"\n'
        f'         datetime="{format_datetime(start)}">\n'
        "       2026/10/06 01:30   </time>\n</span>\n"
        f'<div data-movie-id="{MOVIE}" data-csrf-token="x" {account} data-lang="en"></div>\n'
        "</body></html>"
    )


class FakeYtdlp:
    """Stands in for yt_dlp.YoutubeDL: answers with `raw`, writes the video when asked."""

    raw: dict = {}
    calls: list[tuple[str, dict, bool]] = []

    def __init__(self, opts: dict):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool = True) -> dict:
        FakeYtdlp.calls.append((url, self.opts, download))
        if download:
            target = self.opts["outtmpl"].replace(".%(ext)s", ".mp4").replace("%%", "%")
            Path(target).write_bytes(b"video")
        return dict(self.raw)

    def sanitize_info(self, raw: dict) -> dict:
        return raw


def ytdlp_raw(movie: str = MOVIE, title: str = "Night chat / Mika's room", start: datetime = START) -> dict:
    """What yt-dlp reports for a published TwitCasting replay (its title is the full og:title)."""
    return {"id": movie, "title": title, "uploader_id": CHANNEL, "timestamp": int(start.timestamp()),
            "duration": 2383.0, "extractor_key": "TwitCasting", "webpage_url": REPLAY}


class Site:
    """A fake twitcasting.tv: address -> page; remembers every request."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        page = self.pages.get(str(request.url))
        return httpx.Response(200, text=page) if page is not None else httpx.Response(404, text="not found")


@pytest.fixture
def tc(tmp_path, monkeypatch):
    """Settings with a cookies file, a fake site serving a published replay, a fake yt-dlp."""
    cookies = tmp_path / "twitcasting_cookies.txt"
    name, value = COOKIE.split("=")
    cookies.write_text("# Netscape HTTP Cookie File\n"
                       f".twitcasting.tv\tTRUE\t/\tTRUE\t2147483647\t{name}\t{value}\n", encoding="utf-8")
    youtube_cookies = tmp_path / "youtube_cookies.txt"
    youtube_cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    settings = DownloadSettings(output_dir=tmp_path / "dl", twitcasting_cookies_file=cookies,
                                cookies_file=youtube_cookies, cookies_from_browser="firefox")
    site = Site({REPLAY: replay_page()})
    monkeypatch.setattr(TwitCastingDownloader, "transport", httpx.MockTransport(site))
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYtdlp)
    monkeypatch.setattr(FakeYtdlp, "raw", ytdlp_raw())
    monkeypatch.setattr(FakeYtdlp, "calls", [])

    return SimpleNamespace(settings=settings, site=site, cookies=cookies, tmp=tmp_path)
