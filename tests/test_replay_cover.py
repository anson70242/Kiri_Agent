"""The replay's own cover: the downloader reports it from the replay's metadata, and once the
replay of a Twitch live is downloaded, 「回放已下载」 and every later message of that live carry
it in place of the live preview (or the streamer's avatar when there is none)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from backend.archive import Archive, Artifact
from backend.downloader import auto as auto_mod
from backend.downloader.auto import AutoDownloader
from backend.downloader.base import DownloadResult, DownloadSettings, VideoInfo
from backend.downloader.twitcasting import TwitCastingDownloader
from backend.downloader.twitch import TwitchDownloader
from backend.downloader.youtube import YoutubeDownloader
from backend.drive import DriveCopy, DriveFailed
from backend.monitor import Streamer
from backend.notify import EventKind, StreamEvent

from .helpers import HIGHLIGHTS, A, B, bare_archive, later, live

# Made-up addresses, shaped like each platform's.
REPLAY_COVER = "https://static-cdn.example/cf_vods/abc123/thumb/thumb0-1280x720.jpg"
TWITCH_PROCESSING = "https://vod-secure.example/_404/404_processing_320x180.png"
LIVE_PREVIEW = "https://static-cdn.example/previews-ttv/live_user_b-1280x720.jpg?t=1"  # a camera after the live
AVATAR = "https://static-cdn.example/user-default-pictures/b-profile_image-300x300.png"

# ---- the downloader's reading of the metadata ----

TWITCH_RAW = {"id": "v42", "uploader_id": "b", "uploader": "B", "upload_date": "20261110",
              "extractor_key": "TwitchVod"}


def test_the_twitch_downloader_reports_the_replays_cover():
    info = TwitchDownloader(DownloadSettings()).parse_info(TWITCH_RAW | {"thumbnail": REPLAY_COVER}, "u")
    assert info.cover == REPLAY_COVER


def test_a_twitch_placeholder_is_reported_as_no_cover():
    info = TwitchDownloader(DownloadSettings()).parse_info(TWITCH_RAW | {"thumbnail": TWITCH_PROCESSING}, "u")
    assert info.cover == ""


@pytest.mark.parametrize("cls", [TwitchDownloader, YoutubeDownloader, TwitCastingDownloader])
def test_metadata_without_a_cover_reports_none(cls):
    info = cls(DownloadSettings()).parse_info({"id": "x", "uploader_id": "b", "upload_date": "20261110"}, "u")
    assert info.cover == ""


@pytest.mark.parametrize("cls", [YoutubeDownloader, TwitCastingDownloader])
def test_other_platforms_report_their_replays_cover_too(cls):
    cover = "https://img.example/vi/x/maxresdefault_404.jpg"  # only Twitch's addresses are judged
    info = cls(DownloadSettings()).parse_info({"id": "x", "uploader_id": "b", "upload_date": "20261110",
                                               "thumbnail": cover}, "u")
    assert info.cover == cover


# ---- the pipeline's messages ----

C = Streamer("C", "twitcasting", "c")
SHEET = "https://docs.example/sheet/abc"
DRIVE_COPY = DriveCopy("录播/B/v.zip", "https://drive.example/s/abc")


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class ReplaySource:
    """Finds each live's replay at the live's own address; its metadata offers `cover`."""

    def __init__(self, cover: str):
        self.cover = cover

    def find_replay(self, url, channel, live_id, started_at):
        return url

    def info(self, url):
        return VideoInfo("twitch", "42", url, "title v1", "b", "B", "20261110", live_status="was_live",
                         cover=self.cover)


def fake_download(url, settings, streamers, info=None, progress=None):
    folder = bare_archive(settings.output_dir, "B", "20261110", "歌枠", stem="v", video=b"x", chat=[])
    return DownloadResult(info, folder, video=Archive.from_folder(folder).video)


class Drive:
    """Fails its first `fail` uploads, then uploads."""

    def __init__(self, fail: int = 0):
        self.fail = fail

    def __call__(self, archive, folder, progress):
        if self.fail:
            self.fail -= 1
            raise DriveFailed("the drive refused")
        return DRIVE_COPY


def analyze(video, streamer, title):
    return Archive.from_video(video).save(Artifact.HIGHLIGHTS, HIGHLIGHTS)


def make(tmp_path, monkeypatch, cover=REPLAY_COVER, stages=True, failing=None, drive=None):
    """The automatic download for A (YouTube), B (Twitch) and C (TwitCasting), its messages
    collected in a list; the replay's metadata offers `cover`. With `stages`, every stage
    after the download is on, B uploads its 路灯表 and is ticked for the drive; `failing`:
    the stage that fails, at its first try for good."""
    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: ReplaySource(cover))
    monkeypatch.setattr(auto_mod, "download", fake_download)
    sent: list[StreamEvent] = []
    settings = DownloadSettings(output_dir=tmp_path / "dl", pending_file=tmp_path / "pending.json")

    def stage(name, fn):
        def run(*args, **kw):
            if failing == name:
                raise RuntimeError(f"{name} broke")
            return fn(*args, **kw)
        return run if stages else None

    ad = AutoDownloader(settings, [A, B, C], {A.id: 6, B.id: 6, C.id: 6}, tmp_path / "queue.json",
                        retry_interval=900, max_errors=1, notify=sent.append, clock=Clock(),
                        transcribe=stage("transcribe", lambda video, **_: None),
                        analyze=stage("analyze", analyze), translate=stage("translate", lambda video: None),
                        upload=stage("upload", lambda table, sheet, streamer, title: SHEET),
                        drive=(drive or Drive()) if stages else None)
    if stages:
        ad.uploads = {B.id: "sheet-id"}
        ad.drive_folders = {B.id: "录播/B"}
    return ad, sent


def ended(streamer=B, avatar=AVATAR):
    """A live of `streamer` ended, as the monitor reported it: the live preview as its cover, the avatar."""
    status = dataclasses.replace(live(streamer, sid="v1"), thumbnail=LIVE_PREVIEW, avatar=avatar)
    return StreamEvent(EventKind.OFFLINE, status, later(60))


def run_due(ad):
    """Every stage that is due, until none is (a failed drive upload is retried at once)."""
    while job := ad.next_due():
        ad.finish(job, ad.attempt(job))
        ad.clock.t += 900


def covers(sent):
    return [(e.kind, e.status.thumbnail) for e in sent]


def test_replay_downloaded_of_an_ended_twitch_live_carries_the_replays_cover(tmp_path, monkeypatch):
    ad, sent = make(tmp_path, monkeypatch, stages=False)
    ad.on_ended(ended())
    run_due(ad)

    assert covers(sent) == [(EventKind.DOWNLOADED, REPLAY_COVER), (EventKind.READY, REPLAY_COVER)]


def test_every_later_message_of_the_live_carries_the_replays_cover(tmp_path, monkeypatch):
    ad, sent = make(tmp_path, monkeypatch)
    ad.on_ended(ended())
    run_due(ad)

    assert covers(sent) == [(EventKind.DOWNLOADED, REPLAY_COVER), (EventKind.ANALYZED, REPLAY_COVER),
                            (EventKind.UPLOADED, REPLAY_COVER), (EventKind.READY, REPLAY_COVER),
                            (EventKind.DRIVE_UPLOADED, REPLAY_COVER)]


@pytest.mark.parametrize("stage, failure", [
    ("transcribe", EventKind.TRANSCRIBE_FAILED), ("analyze", EventKind.ANALYZE_FAILED),
    ("translate", EventKind.TRANSLATE_FAILED), ("upload", EventKind.UPLOAD_FAILED),
    ("drive", EventKind.DRIVE_FAILED)])
def test_a_later_stages_failure_carries_the_replays_cover(tmp_path, monkeypatch, stage, failure):
    ad, sent = make(tmp_path, monkeypatch, failing=stage, drive=Drive(fail=1 if stage == "drive" else 0))
    ad.on_ended(ended())
    run_due(ad)

    assert failure in [kind for kind, _ in covers(sent)]
    assert {cover for _, cover in covers(sent)} == {REPLAY_COVER}


@pytest.mark.parametrize("cover", ["", TWITCH_PROCESSING], ids=["no cover", "a placeholder"])
def test_without_the_replays_cover_the_messages_carry_the_avatar(tmp_path, monkeypatch, cover):
    # The Twitch downloader reports a placeholder as no cover; the source here is past it.
    info = TwitchDownloader(DownloadSettings()).parse_info(TWITCH_RAW | {"thumbnail": cover}, "u")
    ad, sent = make(tmp_path, monkeypatch, cover=info.cover)
    ad.on_ended(ended())
    run_due(ad)

    assert len(sent) == 5 and {c for _, c in covers(sent)} == {AVATAR}


def test_without_an_avatar_either_the_messages_carry_no_picture(tmp_path, monkeypatch):
    ad, sent = make(tmp_path, monkeypatch, cover="")
    ad.on_ended(ended(avatar=None))
    run_due(ad)

    assert len(sent) == 5 and {c for _, c in covers(sent)} == {None}


@pytest.mark.parametrize("cover, picture", [(REPLAY_COVER, REPLAY_COVER), ("", AVATAR)],
                         ids=["the replay's cover", "the avatar"])
def test_a_service_started_again_sends_the_later_messages_with_the_same_picture(
        tmp_path, monkeypatch, cover, picture):
    ad, sent = make(tmp_path, monkeypatch, cover=cover)
    ad.on_ended(ended())
    job = ad.next_due()
    ad.finish(job, ad.attempt(job))  # downloaded: the transcription is next
    assert covers(sent) == [(EventKind.DOWNLOADED, picture)]

    again, sent_again = make(tmp_path, monkeypatch, cover="https://elsewhere.example/other.jpg")
    run_due(again)

    assert len(sent_again) == 4 and {c for _, c in covers(sent_again)} == {picture}


def test_a_queue_file_from_before_replay_covers_still_loads(tmp_path, monkeypatch):
    ad, _ = make(tmp_path, monkeypatch)
    ad.on_ended(ended())
    path = tmp_path / "queue.json"
    jobs = json.loads(path.read_text(encoding="utf-8"))
    for job in jobs:
        job.pop("replay_cover")
    path.write_text(json.dumps(jobs), encoding="utf-8")

    again, sent = make(tmp_path, monkeypatch)
    run_due(again)
    assert {c for _, c in covers(sent)} == {REPLAY_COVER}


@pytest.mark.parametrize("streamer", [A, C], ids=["YouTube", "TwitCasting"])
def test_other_platforms_messages_carry_the_monitors_cover_as_before(tmp_path, monkeypatch, streamer):
    ad, sent = make(tmp_path, monkeypatch, stages=False)  # their replay's metadata offers a cover too
    ad.on_ended(ended(streamer))
    run_due(ad)

    assert covers(sent) == [(EventKind.DOWNLOADED, LIVE_PREVIEW), (EventKind.READY, LIVE_PREVIEW)]

