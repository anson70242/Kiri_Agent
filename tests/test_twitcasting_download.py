"""Downloading a TwitCasting replay, through the download entry point, with the site and
yt-dlp faked.

The replay pages keep only the markers verified on the real site (2026-11-13): the
`data-movie-playlist` attribute of a published replay (seen only when logged in), the
`data-account-type` of the visitor, the player's empty message, a password form, the
player's `<time datetime>` and `og:title`. Names, titles and cookie values are invented
(the repository is public).
"""

from __future__ import annotations

import json
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from yt_dlp.utils import DownloadError as YtdlpError

from backend.downloader import REPO_ROOT, DownloadError, DownloadSettings, ReplayUnpublished, download, load_settings
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

    class Env:
        pass

    env = Env()
    env.settings, env.site, env.cookies, env.tmp = settings, site, cookies, tmp_path
    return env


def test_a_published_replay_lands_as_one_video_in_the_creators_archive(tc):
    r = download(REPLAY, tc.settings, [MIKA], chat=False)

    assert r.ok
    assert r.output_dir == tc.tmp / "dl" / "Mika" / "20261006" / "Night chat 013044"
    assert r.video == r.output_dir / "[20261006][Mika] Night chat 013044.mp4"
    assert r.video.read_bytes() == b"video"


def test_every_replay_request_carries_the_twitcasting_cookies_and_never_the_youtube_ones(tc):
    download(REPLAY, tc.settings, [MIKA], chat=False)

    assert [str(q.url) for q in tc.site.requests] == [REPLAY]
    assert COOKIE in tc.site.requests[0].headers["cookie"]
    assert [(url, download) for url, _, download in FakeYtdlp.calls] == [(REPLAY, False), (REPLAY, True)]
    for _, opts, _ in FakeYtdlp.calls:
        assert opts["cookiefile"] == str(tc.cookies)
        assert "cookiesfrombrowser" not in opts


def test_a_failing_video_is_not_retried_with_other_cookies(tc, monkeypatch):
    def fail(self, url, download=True):
        FakeYtdlp.calls.append((url, self.opts, download))
        if download:
            raise YtdlpError("ERROR: [TwitCasting] 900000001: Failed to get m3u8 playlist")
        return dict(self.raw)

    monkeypatch.setattr(FakeYtdlp, "extract_info", fail)
    r = download(REPLAY, tc.settings, [MIKA], chat=False)

    assert r.errors == {"video": "[TwitCasting] 900000001: Failed to get m3u8 playlist"}
    assert len(FakeYtdlp.calls) == 2  # the metadata, then one try at the video


def test_the_chat_is_not_supported_yet_but_the_video_still_lands(tc):
    r = download(REPLAY, tc.settings, [MIKA])

    assert r.video.exists() and r.chat is None
    assert "not supported yet" in r.errors["chat"]
    assert set(r.errors) == {"chat"}


# ---- archive ----

OTHER_MOVIE = "900000002"
OTHER_REPLAY = f"https://twitcasting.tv/{CHANNEL}/movie/{OTHER_MOVIE}"


def test_two_lives_with_the_same_title_on_one_day_are_two_archives(tc, monkeypatch):
    later = datetime(2026, 10, 6, 21, 5, 9).astimezone()
    tc.site.pages[OTHER_REPLAY] = replay_page(start=later)
    first = download(REPLAY, tc.settings, [MIKA], chat=False)
    monkeypatch.setattr(FakeYtdlp, "raw", ytdlp_raw(OTHER_MOVIE, start=later))
    second = download(OTHER_REPLAY, tc.settings, [MIKA], chat=False)

    assert first.output_dir.name == "Night chat 013044"
    assert second.output_dir.name == "Night chat 210509"
    assert first.video.exists() and second.video.exists()


def test_downloading_the_same_replay_twice_skips_the_video_the_second_time(tc):
    first = download(REPLAY, tc.settings, [MIKA], chat=False)
    second = download(REPLAY, tc.settings, [MIKA], chat=False)

    assert second.video == first.video
    assert [download for _, _, download in FakeYtdlp.calls].count(True) == 1


def test_a_live_without_its_own_title_keeps_the_channel_title(tc):
    tc.site.pages[REPLAY] = replay_page(og_title="Mika's room")
    r = download(REPLAY, tc.settings, [MIKA], chat=False)
    assert r.output_dir.name == "Mika's room 013044"


@pytest.mark.parametrize("roster_channel", ["@mika_test01", "https://twitcasting.tv/Mika_Test01"])
def test_the_creator_is_the_roster_entry_of_the_replays_channel(tc, roster_channel):
    others = [Streamer("Someone", "twitcasting", "someone_else"), Streamer("Mika (YT)", "youtube", "@mika_test01")]
    r = download(REPLAY, tc.settings, [*others, Streamer("Mika", "twitcasting", roster_channel)], chat=False)
    assert r.output_dir.parts[-3] == "Mika"


def test_a_channel_outside_the_roster_is_filed_under_its_channel_id(tc):
    r = download(REPLAY, tc.settings, [], chat=False)
    assert r.output_dir.parts[-3] == CHANNEL


# ---- addresses given by hand ----

def test_a_channel_address_is_refused_with_a_request_for_a_replay_address(tc):
    with pytest.raises(DownloadError, match="replay address"):
        download(f"https://twitcasting.tv/{CHANNEL}", tc.settings, [MIKA])
    assert tc.site.requests == [] and FakeYtdlp.calls == []


def run_command_line(tc, monkeypatch, *args: str) -> int:
    from backend.downloader import __main__ as cli

    cfg = tc.tmp / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "Mika", "platform": "twitcasting", "channel": CHANNEL}]},
        "download": {"output_dir": str(tc.tmp / "dl"), "twitcasting_cookies_file": str(tc.cookies),
                     "chat_retry": {"state_file": str(tc.tmp / "pending.json")}},
    }), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["backend.downloader", "--config", str(cfg), *args])
    return cli.main()


def test_the_command_line_downloads_a_replay_address(tc, monkeypatch, capsys):
    assert run_command_line(tc, monkeypatch, REPLAY, "--no-chat") == 0
    video = tc.tmp / "dl" / "Mika" / "20261006" / "Night chat 013044" / "[20261006][Mika] Night chat 013044.mp4"
    assert video.exists() and str(video) in capsys.readouterr().out
    assert COOKIE in tc.site.requests[0].headers["cookie"]  # the cookies file named in config.json


def test_the_command_line_refuses_a_channel_address(tc, monkeypatch, capsys):
    assert run_command_line(tc, monkeypatch, f"https://twitcasting.tv/{CHANNEL}") == 1
    assert "replay address" in capsys.readouterr().err


def test_the_twitcasting_cookies_file_defaults_to_the_data_directory(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"monitor": {"streamers": []}}), encoding="utf-8")
    settings, _ = load_settings(cfg)
    assert settings.twitcasting_cookies_file == REPO_ROOT / "data" / "twitcasting_cookies.txt"


def test_a_replay_still_on_air_is_refused(tc, monkeypatch):
    monkeypatch.setattr(FakeYtdlp, "raw", ytdlp_raw() | {"live_status": "is_live"})
    with pytest.raises(DownloadError, match="is_live"):
        download(REPLAY, tc.settings, [MIKA])
    no_archive_and_no_video_asked(tc)


# ---- replay state ----

def no_archive_and_no_video_asked(tc):
    assert not (tc.tmp / "dl").exists()
    assert [download for _, _, download in FakeYtdlp.calls if download] == []


def test_a_replay_that_is_not_published_is_its_own_outcome(tc):
    tc.site.pages[REPLAY] = replay_page("unpublished")
    with pytest.raises(ReplayUnpublished):
        download(REPLAY, tc.settings, [MIKA])
    no_archive_and_no_video_asked(tc)


@pytest.mark.parametrize("message", ["Login required to watch", "ログインが必要です"])
def test_a_page_without_the_login_is_a_login_failure_in_any_language(tc, message):
    tc.site.pages[REPLAY] = replay_page("login", message=message)
    with pytest.raises(DownloadError, match="(?i)login.*cookies") as e:
        download(REPLAY, tc.settings, [MIKA])
    assert not isinstance(e.value, ReplayUnpublished)
    no_archive_and_no_video_asked(tc)


def test_a_missing_cookies_file_is_a_login_failure_without_asking_the_site(tc):
    tc.cookies.unlink()
    with pytest.raises(DownloadError, match="(?i)login.*cookies"):
        download(REPLAY, tc.settings, [MIKA])
    assert tc.site.requests == []
    no_archive_and_no_video_asked(tc)


def test_a_password_protected_replay_is_not_supported(tc):
    tc.site.pages[REPLAY] = replay_page("password")
    with pytest.raises(DownloadError, match="not supported") as e:
        download(REPLAY, tc.settings, [MIKA])
    assert not isinstance(e.value, ReplayUnpublished)
    no_archive_and_no_video_asked(tc)


def test_an_unrecognised_replay_page_fails_with_the_pages_own_message(tc):
    tc.site.pages[REPLAY] = replay_page("other", message="This live is for members only")
    with pytest.raises(DownloadError, match="This live is for members only") as e:
        download(REPLAY, tc.settings, [MIKA])
    assert "not supported" in str(e.value) and not isinstance(e.value, ReplayUnpublished)
    no_archive_and_no_video_asked(tc)
