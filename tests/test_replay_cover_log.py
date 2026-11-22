"""The log tells, for each live whose replay is downloaded, what the replay's metadata offered as
its cover: a cover, one of Twitch's placeholders, or none. Never the address itself."""

from __future__ import annotations

import logging

import pytest

from backend.downloader import auto as auto_mod
from backend.downloader.base import DownloadSettings
from backend.downloader.twitch import TwitchDownloader

from .test_replay_cover import REPLAY_COVER, TWITCH_PROCESSING, TWITCH_RAW, ended, make, run_due


class TwitchReplay:
    """The replay of the live as the Twitch downloader reads its metadata, offering `thumbnail`."""

    def __init__(self, thumbnail: str | None):
        self.raw = TWITCH_RAW | {"title": "title v1", "live_status": "was_live"}
        if thumbnail is not None:
            self.raw["thumbnail"] = thumbnail

    def find_replay(self, url, channel, live_id, started_at):
        return url

    def info(self, url):
        return TwitchDownloader(DownloadSettings()).parse_info(self.raw, url)


@pytest.mark.parametrize("thumbnail, said", [
    (REPLAY_COVER, "its metadata offered a cover"),
    (TWITCH_PROCESSING, "its metadata offered only a placeholder, taken as no cover"),
    (None, "its metadata offered no cover"),
], ids=["a cover", "a placeholder", "none"])
def test_the_log_tells_what_the_replay_offered_as_its_cover_without_the_address(
        tmp_path, monkeypatch, caplog, thumbnail, said):
    ad, _ = make(tmp_path, monkeypatch, stages=False)
    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: TwitchReplay(thumbnail))
    ad.on_ended(ended())
    with caplog.at_level(logging.INFO, logger="backend.downloader"):
        run_due(ad)

    lines = [r.getMessage() for r in caplog.records if "its metadata offered" in r.getMessage()]
    assert len(lines) == 1 and lines[0].endswith(said) and "B" in lines[0]
    assert not any(address in caplog.text for address in (REPLAY_COVER, TWITCH_PROCESSING))
