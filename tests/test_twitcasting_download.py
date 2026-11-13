"""Downloading a TwitCasting replay, through the download entry point, with the site and
yt-dlp faked (tests/twitcasting_fakes.py)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from yt_dlp.utils import DownloadError as YtdlpError

from backend.downloader import REPO_ROOT, DownloadError, ReplayUnpublished, download, load_settings
from backend.monitor import Streamer

from .twitcasting_fakes import CHANNEL, COOKIE, MIKA, REPLAY, FakeYtdlp, replay_page, tc, ytdlp_raw  # noqa: F401 (tc: fixture)


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
