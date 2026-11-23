"""The flash stage: a ticked streamer's processed live is put on QQ's flash transfer, and
"全部处理完成" goes out once the upload is finished, with the flash link in place of 全部下载
and a line saying until when it works. Through the pipeline with a fake clock and a fake
flash service."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.archive import Archive, Downloads
from backend.flash.client import StartedTransfer
from backend.notify import EventKind

from .flash_stage_fakes import (EXPIRES, FLASH_LINK, MADE, FakeFlash, finished, flash_pipeline, flash_ready_links,
                                processed_live, ready, run_flash, stages, uploading)
from .helpers import A, B, sample_archive
from .test_drive_stage import downloaded, kinds, links, ready_with_the_sites_links, step


def test_a_ticked_streamers_processed_live_is_ready_only_once_its_flash_transfer_is_uploaded(tmp_path):
    flash = FakeFlash(uploading(10), uploading(60), finished())
    ad, clock, sent, _ = processed_live(tmp_path, flash)

    assert stages(ad) == ["flash"]
    assert kinds(sent) == [EventKind.DOWNLOADED]  # no "全部处理完成" before the upload

    run_flash(ad, clock)

    assert ad.load() == []
    assert len(flash.starts) == 1 and flash.looks == ["fs-1", "fs-1", "fs-1"]
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    event = ready(sent)
    assert links(event) == flash_ready_links()
    assert event.detail == "影片 ✅ · 弹幕 ✅ · 日文字幕 ❌ · 中文字幕 ❌ · 路灯表 ❌\n闪传链接有效至 2026/12/7"


# ---- what goes up, and what is left ----

def test_the_flash_transfer_sends_the_archives_files_and_the_archive_keeps_its_flash_link(tmp_path):
    flash = FakeFlash()
    ad, clock, sent = flash_pipeline(tmp_path, flash)
    folder = downloaded(ad)
    sample_archive(ad.settings.output_dir, "A", "20261110", "歌枠", stem="v", video=b"x")  # its transcripts and table
    step(ad, "transcribe")
    run_flash(ad, clock)

    ((name, files),) = flash.starts
    assert name == "v"
    assert sorted(files) == ["v.chat_parsed.json", "v.highlights.xlsx", "v.ja.srt", "v.mp4", "v.zh.srt"]
    assert flash.existed == [True]
    link = Archive.from_folder(folder).read_flash_link()
    assert (link.url, link.expires) == (FLASH_LINK, EXPIRES)
    assert not Downloads(ad.settings.output_dir).flash_staging.exists()  # the subtitles and the sheet are gone
    assert "路灯表 ✅" in ready(sent).detail


def test_the_expiry_line_is_the_day_in_hong_kong(tmp_path):
    flash = FakeFlash(start=[StartedTransfer("fs-9", FLASH_LINK, datetime(2026, 12, 6, 16, 0, tzinfo=timezone.utc), MADE)])
    ad, clock, sent, _ = processed_live(tmp_path, flash)
    run_flash(ad, clock)

    assert ready(sent).detail.endswith("\n闪传链接有效至 2026/12/7")  # midnight in Hong Kong


# ---- where it is off ----

@pytest.mark.parametrize("ticked, service", [((), FakeFlash()), ((A,), None)],
                         ids=["the streamer is not ticked", "no flash service is configured"])
def test_without_a_flash_transfer_ready_goes_out_at_once_with_the_short_download_link(tmp_path, ticked, service):
    ad, _, sent, _ = processed_live(tmp_path, service, ticked=ticked)

    assert ad.load() == []
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    ready_with_the_sites_links(sent)
    assert "闪传" not in ready(sent).detail


def test_a_live_of_an_unticked_streamer_is_not_held_by_a_ticked_ones_flash_transfer(tmp_path):
    flash = FakeFlash(uploading())
    ad, _, sent, _ = processed_live(tmp_path, flash)
    downloaded(ad, B)
    step(ad, "transcribe")
    step(ad, "flash")  # A's flash transfer starts and goes on uploading

    assert [e.status.streamer for e in sent if e.kind is EventKind.READY] == [B]
    assert stages(ad) == ["flash"]


# ---- the queue view ----

def test_the_queue_view_shows_the_flash_job_and_its_upload_with_its_progress(tmp_path):
    flash = FakeFlash(uploading(50, 200), finished(200))
    ad, clock, _, _ = processed_live(tmp_path, flash)
    assert [row["stage"] for row in ad.snapshot()["queue"]] == ["flash"]

    step(ad, "flash")  # started
    step(ad, "flash")  # uploading
    (active,) = ad.snapshot()["active"]
    assert active["stage"] == "flash" and active["title"] == "title v1"
    assert (active["downloaded"], active["total"], active["percent"]) == (50, 200, 25.0)

    run_flash(ad, clock)
    assert ad.snapshot() == {"active": [], "queue": []}
