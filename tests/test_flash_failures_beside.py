"""A failing flash transfer holds back nothing but its own live's "全部处理完成": not other
lives' flash transfers, not the drive copy, not the downloads; its archive is kept until it is
given up on; its tries survive a restart; and the owner's alert reaches Discord's operator
alert. Through the pipeline with a fake clock, a fake drive and a fake flash service, and the
dashboard built from a written config."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.archive import Archive
from backend.downloader import auto as auto_mod
from backend.downloader.base import DownloadResult
from backend.flash.client import AccountState, FlashFailed
from backend.monitor.server import create_app
from backend.notify import EventKind, StreamEvent

from .flash_stage_fakes import (PAUSE, FakeFlash, alerts_of, flash_link, flash_pipeline, processed_live, ready,
                                refused, run_flash, stages, uploading)
from .helpers import A, B, later, live, sample_archive
from .test_drive_stage import DRIVE_LINK, FakeDrive, ReplaySource, downloaded, kinds, links, rewrite_queue, step
from .test_flash_account_api import FakeFlashClient, use_flash
from .test_notify_api import write_config


def ready_links(sent):
    """Each "全部处理完成"'s streamer and first link, in the order they went out."""
    return [(e.status.streamer, e.links[0].url) for e in sent if e.kind is EventKind.READY]


# ---- nothing else waits ----

def test_other_lives_flash_transfers_and_downloads_go_on_while_a_failed_one_waits_for_its_next_try(tmp_path):
    flash = FakeFlash(start=[FlashFailed("闪传服务拒绝了")])
    ad, clock, sent = flash_pipeline(tmp_path, flash, ticked=(A, B))
    downloaded(ad, A)
    step(ad, "transcribe")
    step(ad, "flash")  # A's first try fails: its next one is in fifteen minutes
    downloaded(ad, B)
    step(ad, "transcribe")

    step(ad, "flash")  # B's starts at once
    step(ad, "flash")  # and is uploaded
    assert ready_links(sent) == [(B, flash_link(2))]
    ad.on_ended(StreamEvent(EventKind.OFFLINE, live(A, sid="v2"), later(90)))
    assert ad.next_due("download") is not None  # a new live is downloaded at once

    clock.t += PAUSE
    step(ad, "flash")
    step(ad, "flash")
    assert ready_links(sent) == [(B, flash_link(2)), (A, flash_link(3))]


def test_a_failed_try_while_uploading_lets_the_next_live_start_during_the_pause(tmp_path):
    flash = FakeFlash(uploading(), FlashFailed("闪传服务没有给出闪传的进度"), FlashFailed("x"))
    ad, clock, sent = flash_pipeline(tmp_path, flash, ticked=(A, B))
    downloaded(ad, A)
    step(ad, "transcribe")
    downloaded(ad, B)
    step(ad, "transcribe")
    step(ad, "flash")  # A's started
    step(ad, "flash")  # uploading
    clock.t += 10
    step(ad, "flash")  # its progress cannot be read: a failed try

    step(ad, "flash")  # B's starts
    assert [j.status.streamer for j in ad.load() if j.transfer_id] == [B]
    assert ready_links(sent) == []


def test_the_drive_copy_is_uploaded_while_the_flash_transfer_fails_and_ready_goes_out_once(tmp_path):
    drive = FakeDrive()
    ad, clock, sent, _ = processed_live(tmp_path, refused(3), folders={"youtube:@a": "录播/A"}, drive=drive)
    step(ad, "flash")  # the first try fails
    step(ad, "drive")

    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.DRIVE_UPLOADED]
    assert links(sent[-1]) == [DRIVE_LINK]
    run_flash(ad, clock)
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.DRIVE_UPLOADED, EventKind.READY]
    assert ready(sent).links[0].label == "全部下载（影片、弹幕、中日字幕 SRT）"


def test_a_drive_copy_after_a_given_up_flash_transfer_sends_no_second_ready(tmp_path):
    ad, clock, sent, _ = processed_live(tmp_path, refused(3), folders={"youtube:@a": "录播/A"}, drive=FakeDrive())
    run_flash(ad, clock)
    assert stages(ad) == ["drive"]
    step(ad, "drive")

    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY, EventKind.DRIVE_UPLOADED]


# ---- the archive ----

def test_the_archive_stays_while_its_flash_transfer_is_retried_and_goes_by_the_usual_rule_once_given_up(
        tmp_path, monkeypatch):
    dates = iter(f"202611{d:02d}" for d in range(1, 31))

    def fake_download(url, settings, streamers, info=None, progress=None):
        folder = sample_archive(settings.output_dir, "A", next(dates), info.id, stem=info.id, video=b"x")
        return DownloadResult(info, folder, video=Archive.from_folder(folder).video)

    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: ReplaySource())
    monkeypatch.setattr(auto_mod, "download", fake_download)
    flash = FakeFlash(start=[FlashFailed("闪传服务拒绝了")] * 3)
    ad, clock, _ = flash_pipeline(tmp_path, flash)
    ad.keep = {A.id: 1}

    def end_and_process(sid):
        ad.on_ended(StreamEvent(EventKind.OFFLINE, live(A, sid=sid), later(60)))
        job = ad.next_due("download")
        outcome = ad.attempt(job)
        ad.finish(job, outcome)
        step(ad, "transcribe")
        return outcome.result.output_dir

    first = end_and_process("v1")
    step(ad, "flash")  # fails: tried again later
    clock.t += 60
    second = end_and_process("v2")
    assert first.exists() and second.exists()

    for _ in range(2):  # the first live's second and third tries: given up on
        clock.t += PAUSE
        step(ad, "flash")
    assert [j.status.stream_id for j in ad.load()] == ["v2"]
    end_and_process("v3")
    assert not first.exists() and second.exists()  # the second still has its flash job


# ---- a restart ----

def test_the_failed_tries_survive_a_restart_and_the_owner_is_alerted_once_at_the_maximum(tmp_path):
    ad, clock, sent, _ = processed_live(tmp_path, refused(1))
    step(ad, "flash")  # the first try fails

    restarted, clock2, sent2 = flash_pipeline(tmp_path, refused(2))
    alerts = alerts_of(restarted)
    clock2.t = clock.t + PAUSE
    step(restarted, "flash")
    assert restarted.load()[0].errors == 2 and alerts == []
    clock2.t += PAUSE
    step(restarted, "flash")

    assert restarted.load() == []
    assert kinds(sent2) == [EventKind.READY] and len(alerts) == 1


def test_a_hung_upload_from_a_queue_file_written_before_this_counts_thirty_minutes_after_the_first_look(tmp_path):
    ad, clock, _, _ = processed_live(tmp_path, FakeFlash(uploading(50)))
    step(ad, "flash")  # started
    rewrite_queue(tmp_path, lambda job: [job.pop(name) for name in ("flash_sent", "flash_moved_at")])

    restarted, clock2, _ = flash_pipeline(tmp_path, FakeFlash(uploading(50)))
    clock2.t = clock.t + 3600
    step(restarted, "flash")  # the first look after the restart: no failed try yet
    assert restarted.load()[0].errors == 0
    clock2.t += 30 * 60
    step(restarted, "flash")
    assert restarted.load()[0].errors == 1


# ---- the operator alert in the running service ----

def test_the_pipeline_alerts_the_owner_through_the_operator_alert(tmp_path, monkeypatch):
    use_flash(monkeypatch, FakeFlashClient(AccountState("online")))
    monkeypatch.setenv("KIRI_T_HOOK", "https://discord.test/hook")
    app = TestClient(create_app(write_config(tmp_path))).app

    assert app.state.auto.alert == app.state.notifier.alert
