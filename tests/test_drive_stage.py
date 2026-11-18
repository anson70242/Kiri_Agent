"""The drive stage: a ticked streamer's finished live is uploaded to the cloud drive, and
"全部处理完成" goes out when that upload is done, with the drive link. Through the automatic
download with a fake clock, fake stages and a fake drive."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import pytest

import backend.downloader
from backend.downloader import auto as auto_mod
from backend.archive import Archive, Artifact
from backend.downloader.auto import AutoDownloader, Outcome, load_auto
from backend.downloader.base import ChatNotReady, DownloadResult, DownloadSettings, VideoInfo
from backend.downloader.pending import PendingChats
from backend.drive import DriveAuthorizationLapsed, DriveCopy, DriveFailed
from backend.notify import EventKind, StreamEvent

from .helpers import A, B, bare_archive, later, live

SITE = "https://kiri.example"
DRIVE_LABEL = "全部下载（夸克网盘：影片、弹幕、中日字幕 SRT、路灯表）"


def site_links(creator="A"):
    """READY's links today, for `<creator>/20261110/歌枠`: the site's zip, then the page."""
    ref = f"{creator}%2F20261110%2F%E6%AD%8C%E6%9E%A0"
    return [(f"{SITE}/api/archives/zip?folder={ref}", "全部下载（影片、弹幕、中日字幕 SRT）"),
            (f"{SITE}/archives#{ref}", "录播页面")]


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class FakeDrive:
    """Records each upload; answers with `copy`, or raises `error`."""

    def __init__(self, copy: DriveCopy | None = None, error: Exception | None = None):
        self.copy = copy or DriveCopy("录播/A/v.zip", "https://drive.example/s/abc?pwd=x1y2")
        self.error = error
        self.calls: list[tuple[Archive, str]] = []
        self.during = None  # called inside the upload, with the progress callback

    def __call__(self, archive, folder, progress):
        self.calls.append((archive, folder))
        if self.during:
            self.during(progress)
        if self.error:
            raise self.error
        return self.copy


def make(tmp_path, drive=None, folders=None, **kw):
    """Automatic download for A and B, with a transcription stage, a fake drive, and A ticked
    for the drive (folder 录播/A) unless `folders` says otherwise."""
    clock = Clock()
    sent: list[StreamEvent] = []
    settings = DownloadSettings(output_dir=tmp_path / "dl", pending_file=tmp_path / "pending.json")
    ad = AutoDownloader(settings, [A, B], {A.id: 6, B.id: 6}, tmp_path / "queue.json",
                        retry_interval=900, notify=sent.append, clock=clock,
                        transcribe=kw.pop("transcribe", lambda video, **_: None), drive=drive, **kw)
    ad.drive_folders = {A.id: "录播/A"} if folders is None else folders
    ad.site_url = SITE
    return ad, clock, sent


def downloaded(ad, s=A, video=b"x"):
    """A live of `s` ends and its archive (`<s>/20261110/歌枠`, video `v.mp4`) is downloaded."""
    ad.on_ended(StreamEvent(EventKind.OFFLINE, live(s, sid="v1"), later(60)))
    folder = bare_archive(ad.settings.output_dir, s.name, "20261110", "歌枠", stem="v", video=video, chat=[])
    info = VideoInfo("youtube", "v1", "u", "title v1", s.channel, s.name, "20261110")
    job = ad.next_due("download")
    ad.finish(job, Outcome("done", result=DownloadResult(info, folder, video=folder / "v.mp4",
                                                         chat=Archive.from_folder(folder).file(Artifact.CHAT))))
    return folder


def step(ad, stage):
    job = ad.next_due(stage)
    assert job is not None, f"no {stage} job due"
    ad.finish(job, ad.attempt(job))


def kinds(sent):
    return [e.kind for e in sent]


def links(event):
    return [(x.url, x.label) for x in event.links]


# ---- queued in place of READY ----

def test_a_ticked_streamers_processed_live_gets_a_drive_job_and_no_ready_yet(tmp_path):
    drive = FakeDrive()
    ad, _, sent = make(tmp_path, drive)
    downloaded(ad)
    step(ad, "transcribe")

    assert [j.stage for j in ad.load()] == ["drive"]
    assert EventKind.READY not in kinds(sent)
    assert drive.calls == []


def ready_as_today(sent, creator="A"):
    (ready,) = [e for e in sent if e.kind is EventKind.READY]
    assert links(ready) == site_links(creator)
    return ready


def test_ready_as_today_for_a_streamer_who_is_not_ticked(tmp_path):
    drive = FakeDrive()
    ad, _, sent = make(tmp_path, drive)
    downloaded(ad, B)
    step(ad, "transcribe")

    assert ad.load() == [] and drive.calls == []
    ready_as_today(sent, "B")


def test_ready_as_today_when_the_drive_is_not_available(tmp_path):
    ad, _, sent = make(tmp_path, drive=None)
    downloaded(ad)
    step(ad, "transcribe")

    assert ad.load() == []
    ready_as_today(sent)


def test_ready_as_today_for_an_archive_without_video(tmp_path):
    drive = FakeDrive()
    ad, _, sent = make(tmp_path, drive, transcribe=lambda video, **_: video.unlink())  # the video is gone
    downloaded(ad)
    step(ad, "transcribe")

    assert ad.load() == [] and drive.calls == []
    ready_as_today(sent)


# ---- the upload ----

def processed(tmp_path, drive, **kw):
    """A's live, downloaded and transcribed: its drive job is next."""
    ad, clock, sent = make(tmp_path, drive, **kw)
    folder = downloaded(ad)
    step(ad, "transcribe")
    return ad, clock, sent, folder


def test_uploaded_with_a_drive_link_ready_goes_out_once_with_the_drive_link_first(tmp_path):
    drive = FakeDrive(DriveCopy("录播/A/[20261110][A] 歌枠.zip", "https://drive.example/s/abc?pwd=x1y2"))
    ad, _, sent, folder = processed(tmp_path, drive)
    step(ad, "drive")

    assert ad.load() == []
    ((archive, drive_folder),) = drive.calls
    assert archive.folder == folder and archive.video == folder / "v.mp4" and drive_folder == "录播/A"
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]  # no 网盘上传完成
    ready = sent[-1]
    assert links(ready) == [("https://drive.example/s/abc?pwd=x1y2", DRIVE_LABEL), site_links()[1]]
    assert ready.detail == "影片 ✅ · 弹幕 ✅ · 日文字幕 ❌ · 中文字幕 ❌ · 路灯表 ❌"


def test_uploaded_without_a_drive_link_ready_as_today_and_drive_copy_uploaded_says_why(tmp_path):
    drive = FakeDrive(DriveCopy("录播/A/[20261110][A] 歌枠.zip", None, "分享被拒绝：文件审核未通过"))
    ad, _, sent, _ = processed(tmp_path, drive)
    step(ad, "drive")

    assert ad.load() == []
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY, EventKind.DRIVE_UPLOADED]
    ready_as_today(sent)
    uploaded = sent[-1]
    assert "录播/A/[20261110][A] 歌枠.zip" in uploaded.detail
    assert "分享被拒绝：文件审核未通过" in uploaded.detail
    assert uploaded.links == ()


# ---- the chat wait ----

class ChatSource:
    """The platform as the chat retry queue sees it: the chat replay is there or not yet."""

    def __init__(self, ready: bool):
        self.ready = ready

    def info(self, url):
        return VideoInfo("youtube", "v1", url, "title v1", "@a", "A", "20261110", live_status="was_live")

    def download_chat(self, info, out_dir):
        if not self.ready:
            raise ChatNotReady("chat replay not available yet")
        path = out_dir / "v.chat_parsed.json"
        path.write_text("[]", encoding="utf-8")
        return path


@pytest.mark.parametrize("chat_ready, give_up", [(True, 48 * 3600), (False, 0)],
                         ids=["the chat arrives", "the chat is given up on"])
def test_the_drive_job_and_ready_wait_for_a_late_chat(tmp_path, monkeypatch, chat_ready, give_up):
    drive = FakeDrive()
    ad, clock, sent = make(tmp_path, drive)
    ad.on_ended(StreamEvent(EventKind.OFFLINE, live(A, sid="v1"), later(60)))
    folder = bare_archive(ad.settings.output_dir, "A", "20261110", "歌枠", stem="v", video=b"x")
    info = VideoInfo("youtube", "v1", "https://x/v1", "title v1", "@a", "A", "20261110")
    PendingChats.from_settings(ad.settings).add(info, folder, "chat replay not available yet")
    ad.finish(ad.next_due("download"),
              Outcome("done", result=DownloadResult(info, folder, video=folder / "v.mp4", chat_queued=True)))
    step(ad, "transcribe")

    for _ in range(3):  # the chat is still awaited: nothing is uploaded, nothing counted
        step(ad, "drive")
        assert ad.next_due("drive") is None
        clock.t += 900
    (job,) = ad.load()
    assert (job.stage, job.attempts, job.errors) == ("drive", 0, 0)
    assert drive.calls == [] and EventKind.READY not in kinds(sent)

    monkeypatch.setattr(backend.downloader, "downloader_for", lambda *a: ChatSource(chat_ready))
    settings = ad.settings
    settings.chat_retry_give_up = give_up
    PendingChats.from_settings(settings).retry([A], settings, force=True)
    step(ad, "drive")

    assert len(drive.calls) == 1 and ad.load() == []
    assert kinds(sent).count(EventKind.READY) == 1
    assert ("弹幕 ✅" in sent[-1].detail) is chat_ready


# ---- unticked meanwhile ----

def test_unticked_before_the_upload_starts_the_job_ends_and_ready_goes_out_as_today(tmp_path):
    drive = FakeDrive()
    ad, _, sent, _ = processed(tmp_path, drive)
    ad.drive_folders = {}  # the owner unticks the box in the gear
    step(ad, "drive")

    assert ad.load() == [] and drive.calls == []
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    ready_as_today(sent)


def test_unticked_while_uploading_the_upload_finishes(tmp_path):
    drive = FakeDrive()
    ad, _, sent, _ = processed(tmp_path, drive)
    drive.during = lambda progress: setattr(ad, "drive_folders", {})
    step(ad, "drive")

    assert len(drive.calls) == 1 and ad.load() == []
    assert links(sent[-1])[0] == ("https://drive.example/s/abc?pwd=x1y2", DRIVE_LABEL)


def test_a_drive_job_left_from_before_a_restart_without_the_drive_ends_with_ready_as_today(tmp_path):
    ad, _, _, _ = processed(tmp_path, FakeDrive())
    restarted, _, sent = make(tmp_path, drive=None)
    step(restarted, "drive")

    assert restarted.load() == []
    assert kinds(sent) == [EventKind.READY]
    ready_as_today(sent)


# ---- until failed uploads are retried ----

@pytest.mark.parametrize("error", [DriveFailed("32003 空间不足"), DriveAuthorizationLapsed("-1408 授权已失效"),
                                   RuntimeError("zip builder broke")], ids=["failed", "authorization lapsed", "a bug"])
def test_an_attempt_that_does_not_upload_ends_the_job_with_ready_as_today_and_a_logged_error(tmp_path, caplog, error):
    drive = FakeDrive(error=error)
    ad, _, sent, _ = processed(tmp_path, drive)
    with caplog.at_level(logging.ERROR):
        step(ad, "drive")

    assert ad.load() == []
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    ready_as_today(sent)
    assert any(str(error) in r.getMessage() and r.levelno >= logging.ERROR for r in caplog.records)


# ---- the queue view ----

def test_the_snapshot_shows_the_drive_job_waiting_then_uploading_with_its_progress(tmp_path):
    drive = FakeDrive()
    ad, _, _, _ = processed(tmp_path, drive)
    assert [row["stage"] for row in ad.snapshot()["queue"]] == ["drive"]
    seen = []

    def upload(progress):
        progress(50, 200)
        seen.append(ad.snapshot()["active"])

    drive.during = upload
    step(ad, "drive")

    ((active,),) = seen
    assert active["stage"] == "drive" and active["title"] == "title v1"
    assert (active["downloaded"], active["total"], active["percent"]) == (50, 200, 25.0)
    assert ad.snapshot() == {"active": [], "queue": []}


# ---- READY once, across a restart ----

def rewrite_queue(tmp_path, change):
    """Edit the queue file's jobs as raw JSON, the way an earlier run could have left it."""
    path = tmp_path / "queue.json"
    jobs = json.loads(path.read_text(encoding="utf-8"))
    for job in jobs:
        change(job)
    path.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")


def test_a_restart_before_the_upload_loses_no_ready(tmp_path):
    processed(tmp_path, FakeDrive())
    restarted, _, sent = make(tmp_path, FakeDrive())
    step(restarted, "drive")

    assert kinds(sent) == [EventKind.READY]
    assert links(sent[0])[0] == ("https://drive.example/s/abc?pwd=x1y2", DRIVE_LABEL)


def test_a_queue_file_from_before_drive_copies_loads(tmp_path):
    processed(tmp_path, FakeDrive())
    rewrite_queue(tmp_path, lambda job: job.pop("ready_sent"))
    restarted, _, sent = make(tmp_path, FakeDrive())

    assert [j.stage for j in restarted.load()] == ["drive"]
    step(restarted, "drive")
    assert kinds(sent) == [EventKind.READY]


def test_a_job_that_already_sent_ready_never_sends_it_again(tmp_path):
    processed(tmp_path, FakeDrive())
    rewrite_queue(tmp_path, lambda job: job.update(ready_sent=True))
    restarted, _, sent = make(tmp_path, FakeDrive(DriveCopy("录播/A/v.zip", "https://drive.example/s/abc?pwd=x1y2")))
    step(restarted, "drive")

    assert kinds(sent) == [EventKind.DRIVE_UPLOADED]  # the drive link comes on its own
    assert links(sent[0]) == [("https://drive.example/s/abc?pwd=x1y2", "网盘链接")]
    assert "录播/A/v.zip" in sent[0].detail


# ---- the archive stays until its drive job is over ----

class ReplaySource:
    """Finds each live's replay at the live's own address, ready to download."""

    def find_replay(self, url, channel, live_id, started_at):
        return url

    def info(self, url):
        sid = url.rsplit("/", 1)[-1]
        return VideoInfo("youtube", sid, url, f"title {sid}", "@a", "A", "20261110", live_status="was_live")


def test_the_archive_stays_while_its_drive_job_is_queued_and_goes_by_the_usual_rule_after(tmp_path, monkeypatch):
    dates = iter(f"202611{d:02d}" for d in range(1, 31))

    def fake_download(url, settings, streamers, info=None, progress=None):
        folder = bare_archive(settings.output_dir, "A", next(dates), info.id, stem=info.id, video=b"x")
        return DownloadResult(info, folder, video=folder / f"{info.id}.mp4")

    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: ReplaySource())
    monkeypatch.setattr(auto_mod, "download", fake_download)
    ad, _, _ = make(tmp_path, FakeDrive())
    ad.keep = {A.id: 1}

    def end_and_process(sid):
        ad.on_ended(StreamEvent(EventKind.OFFLINE, live(A, sid=sid), later(60)))
        job = ad.next_due("download")
        outcome = ad.attempt(job)
        ad.finish(job, outcome)
        step(ad, "transcribe")
        return outcome.result.output_dir

    first = end_and_process("v1")
    assert [j.stage for j in ad.load()] == ["drive"]
    second = end_and_process("v2")
    assert first.exists() and second.exists()  # the first one's drive copy is not uploaded yet

    while ad.next_due("drive"):
        step(ad, "drive")
    third = end_and_process("v3")
    assert not first.exists() and not second.exists() and third.exists()


def test_the_running_service_has_no_drive_yet(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"monitor": {"streamers": [
        {"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True, "drive": True}]}}),
        encoding="utf-8")
    ad = load_auto(cfg)
    assert ad.drive_folders == {"youtube:@a": "录播/A"}
    assert ad.drive is None  # until the drive is wired in: every live is reported READY as today


# ---- its own worker ----

def test_an_upload_holds_up_no_other_stage(tmp_path):
    release, uploading = threading.Event(), threading.Event()
    drive = FakeDrive()
    drive.during = lambda progress: (uploading.set(), release.wait(5))
    transcribed = []
    ad, _, sent, _ = processed(tmp_path, drive, transcribe=lambda video, **_: transcribed.append(video))
    ad.clock = time.time
    downloaded(ad, B)  # B: waiting for transcription

    async def scenario():
        worker = asyncio.create_task(ad.run())
        try:
            assert await asyncio.to_thread(uploading.wait, 3)
            for _ in range(150):  # B is transcribed while A's upload is still running
                await asyncio.sleep(0.02)
                if not any(j.stage == "transcribe" for j in ad.load()):
                    break
            assert [a["stage"] for a in ad.snapshot()["active"]] == ["drive"]
            assert len(transcribed) == 2 and EventKind.READY in kinds(sent)  # B's READY, as today
            release.set()
            for _ in range(150):
                await asyncio.sleep(0.02)
                if not ad.load():
                    break
        finally:
            release.set()
            worker.cancel()

    asyncio.run(scenario())
    assert ad.load() == [] and kinds(sent).count(EventKind.READY) == 2
