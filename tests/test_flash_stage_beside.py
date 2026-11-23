"""The flash stage beside what else waits for a processed live: its late chat, which goes
into the flash transfer, and its drive copy, which neither waits for the flash transfer nor
sends "全部处理完成". Through the pipeline with a fake clock, a fake drive and a fake flash
service."""

from __future__ import annotations

from datetime import datetime

from backend.notify import EventKind

from .flash_stage_fakes import (FakeFlash, chat_comes_in, flash_ready_links, finished, late_chat_live, processed_live,
                                ready, run_flash, stages, uploading)
from .test_drive_hours import HONG_KONG
from .test_drive_stage import DRIVE_LINK, FakeDrive, kinds, links, step


# ---- the late chat ----

def test_the_flash_job_waits_for_a_late_chat_and_sends_the_chat_with_the_flash_transfer(tmp_path, monkeypatch):
    flash = FakeFlash()
    ad, clock, sent, _ = late_chat_live(tmp_path, flash)

    for _ in range(3):  # the chat is still awaited: nothing is started, nothing is said
        step(ad, "flash")
        assert ad.next_due("flash") is None
        clock.t += 60
    assert flash.starts == [] and kinds(sent) == [EventKind.DOWNLOADED]
    (job,) = ad.load()
    assert (job.stage, job.attempts, job.errors) == ("flash", 0, 0)

    chat_comes_in(ad, monkeypatch)
    assert kinds(sent) == [EventKind.DOWNLOADED]  # "全部处理完成" is the flash job's to send
    run_flash(ad, clock)

    ((_, files),) = flash.starts
    assert "v.chat_parsed.json" in files
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    assert "弹幕 ✅" in ready(sent).detail
    assert links(ready(sent)) == flash_ready_links()


def test_a_chat_that_comes_in_is_in_the_flash_transfer_within_a_minute(tmp_path, monkeypatch):
    flash = FakeFlash()
    ad, clock, _, _ = late_chat_live(tmp_path, flash)
    step(ad, "flash")
    chat_comes_in(ad, monkeypatch)

    clock.t += 60
    step(ad, "flash")
    assert len(flash.starts) == 1


# ---- the drive copy beside it ----

def test_a_live_with_both_gets_a_drive_job_and_a_flash_job_in_one_save(tmp_path):
    ad, _, sent, _ = processed_live(tmp_path, folders={"youtube:@a": "录播/A"}, drive=FakeDrive())

    assert sorted(stages(ad)) == ["drive", "flash"]
    assert kinds(sent) == [EventKind.DOWNLOADED]


def test_a_drive_copy_uploaded_first_sends_its_drive_message_and_not_ready(tmp_path):
    flash = FakeFlash(uploading())
    drive = FakeDrive()
    ad, clock, sent, _ = processed_live(tmp_path, flash, folders={"youtube:@a": "录播/A"}, drive=drive)
    step(ad, "flash")  # started, uploading
    step(ad, "flash")
    step(ad, "drive")

    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.DRIVE_UPLOADED]
    assert links(sent[-1]) == [DRIVE_LINK]
    assert stages(ad) == ["flash"]

    flash.progress_answers = [finished()]
    run_flash(ad, clock)
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.DRIVE_UPLOADED, EventKind.READY]
    assert links(ready(sent)) == flash_ready_links()


def test_the_drive_copy_still_waits_for_the_drive_upload_hours_and_ready_does_not(tmp_path):
    drive = FakeDrive()
    ad, clock, sent, _ = processed_live(tmp_path, folders={"youtube:@a": "录播/A"}, drive=drive, drive_hours=(3, 9))
    step(ad, "drive")  # the clock's hour is not in them: the drive job waits for 03:00
    run_flash(ad, clock)
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY] and drive.calls == []

    (job,) = ad.load()
    assert datetime.fromtimestamp(job.next_try, HONG_KONG).hour == 3
    clock.t = job.next_try
    step(ad, "drive")
    assert len(drive.calls) == 1 and ad.load() == []
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY, EventKind.DRIVE_UPLOADED]
