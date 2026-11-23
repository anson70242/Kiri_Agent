"""The flash job in the queue file: the flash transfer it started survives a restart (asked
for its progress again, not started a second time), a queue file written before flash
transfers still loads, and no flash link is written to the log. Through the pipeline with a
fake clock and a fake flash service; a restart is the pipeline built again over the same
queue file and downloads folder."""

from __future__ import annotations

import logging

from backend.notify import EventKind

from .flash_stage_fakes import (FLASH_JOB_FIELDS, FLASH_LINK, FakeFlash, finished, flash_pipeline, flash_ready_links,
                                processed_live, ready, run_flash, stages, uploading)
from .test_drive_stage import DRIVE_LINK, FakeDrive, kinds, links, rewrite_queue, step

def test_a_restart_while_uploading_asks_for_the_same_flash_transfers_progress_and_starts_none(tmp_path):
    before = FakeFlash(uploading())
    ad, clock, _, _ = processed_live(tmp_path, before)
    step(ad, "flash")  # started
    step(ad, "flash")  # uploading
    assert len(before.starts) == 1

    after = FakeFlash(finished())
    restarted, clock2, sent = flash_pipeline(tmp_path, after)
    clock2.t = clock.t + 60
    run_flash(restarted, clock2)

    assert after.starts == [] and after.looks == ["fs-1"]
    assert kinds(sent) == [EventKind.READY]
    assert links(ready(sent)) == flash_ready_links(link=FLASH_LINK)


def test_a_queue_file_from_before_flash_transfers_still_loads(tmp_path):
    processed_live(tmp_path, None, folders={"youtube:@a": "录播/A"}, drive=FakeDrive())  # a drive job
    rewrite_queue(tmp_path, lambda job: [job.pop(name) for name in FLASH_JOB_FIELDS])
    restarted, _, sent = flash_pipeline(tmp_path, folders={"youtube:@a": "录播/A"}, drive=FakeDrive())

    assert stages(restarted) == ["drive"]
    step(restarted, "drive")
    assert kinds(sent) == [EventKind.DRIVE_UPLOADED] and links(sent[0]) == [DRIVE_LINK]


def test_no_flash_link_is_written_to_the_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    ad, clock, sent, _ = processed_live(tmp_path / "uploaded", FakeFlash(uploading(), finished()), full=True)
    run_flash(ad, clock)
    failing, clock2, _, _ = processed_live(tmp_path / "failed", FakeFlash(uploading(), RuntimeError("boom")))
    run_flash(failing, clock2)  # a surprise while its flash transfer, link and all, is in the job

    assert ready(sent).links[0].url == FLASH_LINK
    assert "flash transfer started" in caplog.text and "flash attempt raised" in caplog.text
    assert FLASH_LINK not in caplog.text
