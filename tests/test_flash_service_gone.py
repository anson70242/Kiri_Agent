"""A flash job in the queue when no flash service is configured any more (.env lost it, then a
restart): its "全部处理完成" still waits for a late chat, and the staging folder of a flash
transfer it had started goes when the job is dropped. Through the pipeline with a fake clock."""

from __future__ import annotations

from backend.notify import EventKind

from .flash_stage_fakes import (FakeFlash, chat_comes_in, late_chat_live, processed_live, run_flash, stages,
                                staging_exists, uploading)
from .test_drive_stage import kinds, step


def test_ready_still_waits_for_a_late_chat(tmp_path, monkeypatch):
    ad, clock, sent, _ = late_chat_live(tmp_path)
    ad.flash = None  # .env lost the flash service, then a restart
    step(ad, "flash")

    assert stages(ad) == ["flash"] and EventKind.READY not in kinds(sent)

    chat_comes_in(ad, monkeypatch)
    run_flash(ad, clock)

    assert stages(ad) == [] and kinds(sent).count(EventKind.READY) == 1


def test_the_staging_folder_of_a_started_flash_transfer_goes_when_the_job_is_dropped(tmp_path):
    ad, clock, sent, _ = processed_live(tmp_path, FakeFlash(uploading()), full=True)
    step(ad, "flash")  # started: the subtitles and the sheet are staged
    assert staging_exists(ad)

    ad.flash = None
    run_flash(ad, clock)

    assert stages(ad) == [] and kinds(sent).count(EventKind.READY) == 1
    assert not staging_exists(ad)
