"""A flash transfer that fails for a reason other than the flash account is tried again with
the pipeline's tries and pause, each try a new flash transfer. Through the pipeline with a fake
clock and a fake flash service scripted to fail, to stop moving and to report a failed file."""

from __future__ import annotations

import logging

from backend.flash.client import Progress
from backend.notify import EventKind

from .flash_stage_fakes import (PAUSE, FakeFlash, alerts_of, finished, flash_link, flash_ready_links,
                                processed_live, ready, refused, run_flash, staging_exists, stages, uploading)
from .test_drive_stage import kinds, links, step
from .test_flash_ready_sheet import short_links
from .test_source_record_pipeline import Recorder


def test_a_failed_try_is_tried_again_after_the_pause_and_its_ready_carries_the_flash_link(tmp_path):
    flash = refused(1)
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    alerts = alerts_of(ad)
    step(ad, "flash")  # refused

    (job,) = ad.load()
    assert (job.stage, job.errors) == ("flash", 1)
    assert "闪传服务拒绝了" in job.last_error
    clock.t += PAUSE - 1
    assert ad.next_due("flash") is None  # nothing before the pipeline's pause is over
    clock.t += 1
    run_flash(ad, clock)

    assert len(flash.starts) == 2
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    assert links(ready(sent)) == flash_ready_links(link=flash_link(2))
    assert alerts == []


def test_a_new_try_starts_a_new_flash_transfer_and_the_failed_ones_files_are_gone(tmp_path):
    flash = FakeFlash(Progress(100, 100, done=4, failed=1, finished=True), finished())
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    step(ad, "flash")  # started: fs-1
    assert staging_exists(ad)
    step(ad, "flash")  # a file failed

    (job,) = ad.load()
    assert job.errors == 1 and job.transfer is None
    assert not staging_exists(ad)
    clock.t += PAUSE
    run_flash(ad, clock)

    assert [name for name, _ in flash.starts] == ["v", "v"]
    assert flash.looks == ["fs-1", "fs-2"]
    assert links(ready(sent)) == flash_ready_links(link=flash_link(2))
    assert stages(ad) == []


# ---- given up on ----

def test_at_the_maximum_ready_goes_out_with_the_short_download_link_and_the_owner_is_alerted_once(tmp_path):
    reason = f"闪传服务拒绝了：见 {flash_link(9)}"  # the flash service's words may hold an address
    ad, clock, sent, folder = processed_live(tmp_path, refused(3, reason), full=True)
    ad.source_record = record = Recorder()
    made = short_links(ad)
    alerts = alerts_of(ad)
    run_flash(ad, clock)

    assert ad.load() == []
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    assert links(ready(sent)) == [("https://kiri.example/d/Code000001", "全部下载（影片、弹幕、中日字幕 SRT）"),
                                  ("https://kiri.example/archives#A%2F20261110%2F%E6%AD%8C%E6%9E%A0", "录播页面")]
    assert made == ["A/20261110/歌枠"]
    assert "闪传" not in ready(sent).detail
    ((what, _, values),) = record.told
    assert what == "processed" and values["link"] == "https://kiri.example/d/Code000001" and "flash" not in values
    (alert,) = alerts
    assert "闪传" in alert and "放弃" in alert
    assert "A" in alert and "title v1" in alert and "闪传服务拒绝了" in alert
    assert "A/20261110/歌枠" in alert  # the archive, for the command that makes it by hand
    assert "http" not in alert
    assert not staging_exists(ad)


def test_one_try_short_of_the_maximum_nothing_is_sent_yet(tmp_path):
    ad, clock, sent, _ = processed_live(tmp_path, refused(2))
    alerts = alerts_of(ad)
    step(ad, "flash")
    clock.t += PAUSE
    step(ad, "flash")

    assert ad.load()[0].errors == 2
    assert kinds(sent) == [EventKind.DOWNLOADED] and alerts == []
    clock.t += PAUSE
    run_flash(ad, clock)
    assert links(ready(sent)) == flash_ready_links(link=flash_link(3))


# ---- what counts as a failed try ----

MINUTE = 60
LOOK = 10  # a flash transfer's progress is asked for every ten seconds


def test_an_upload_whose_bytes_sent_do_not_move_for_thirty_minutes_counts_as_a_failed_try(tmp_path):
    flash = FakeFlash(uploading(50), uploading(50), uploading(50), finished())
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    step(ad, "flash")  # started
    step(ad, "flash")  # 50 bytes sent
    clock.t += 30 * MINUTE - LOOK
    step(ad, "flash")  # still 50: not yet
    assert ad.load()[0].errors == 0 and ad.load()[0].transfer is not None
    clock.t += LOOK
    step(ad, "flash")  # still 50, thirty minutes on

    (job,) = ad.load()
    assert job.errors == 1 and job.transfer is None
    assert "30 分钟" in job.last_error
    assert not staging_exists(ad)
    clock.t += PAUSE
    run_flash(ad, clock)
    assert links(ready(sent)) == flash_ready_links(link=flash_link(2))


def test_a_slow_upload_that_keeps_moving_never_counts_as_a_failed_try(tmp_path):
    flash = FakeFlash(*[uploading(n, 1000) for n in range(1, 13)], finished(1000))
    ad, clock, sent, _ = processed_live(tmp_path, flash)
    step(ad, "flash")  # started
    for _ in range(12):  # two hours, a byte every ten minutes
        step(ad, "flash")
        clock.t += 10 * MINUTE
    step(ad, "flash")

    assert ad.load() == []
    assert len(flash.starts) == 1
    assert links(ready(sent)) == flash_ready_links()


def test_an_upload_that_never_sends_a_byte_counts_as_a_failed_try_thirty_minutes_after_its_start(tmp_path):
    flash = FakeFlash(uploading(0), uploading(0), finished())
    ad, clock, _, _ = processed_live(tmp_path, flash)
    step(ad, "flash")  # started
    clock.t += 30 * MINUTE
    step(ad, "flash")

    assert ad.load()[0].errors == 1


def test_an_upload_that_reports_a_failed_file_counts_as_a_failed_try(tmp_path):
    flash = FakeFlash(Progress(40, 100, done=1, failed=1, finished=False), finished())
    ad, clock, sent, _ = processed_live(tmp_path, flash)
    step(ad, "flash")
    step(ad, "flash")

    (job,) = ad.load()
    assert job.errors == 1 and "1 个文件上传失败" in job.last_error
    assert kinds(sent) == [EventKind.DOWNLOADED]


def test_a_failure_while_preparing_the_files_counts_as_a_failed_try(tmp_path):
    flash = FakeFlash()
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    blocker = ad.settings.output_dir / ".flash_staging"
    blocker.write_text("not a folder", encoding="utf-8")  # the staging folder cannot be made
    step(ad, "flash")

    (job,) = ad.load()
    assert job.errors == 1 and "准备闪传的文件失败" in job.last_error
    assert flash.starts == []  # nothing was started
    blocker.unlink()
    clock.t += PAUSE
    run_flash(ad, clock)
    assert links(ready(sent)) == flash_ready_links()


def test_no_flash_link_in_a_failed_tries_reason_is_written_to_the_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    ad, clock, _, _ = processed_live(tmp_path, refused(3, f"闪传服务拒绝了：见 {flash_link(9)}"))
    run_flash(ad, clock)

    assert "flash transfer failed (3/3)" in caplog.text
    assert flash_link(9) not in caplog.text
