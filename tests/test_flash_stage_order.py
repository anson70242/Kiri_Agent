"""Flash transfers are made one at a time, in the order the lives were processed; the archive
of a live with a flash job is kept until it is over; and only lives processed from now on get
one. Through the pipeline with a fake clock and a fake flash service."""

from __future__ import annotations

import asyncio

from backend.notify import EventKind, StreamEvent

from .flash_stage_fakes import (FakeFlash, chat_comes_in, finished, flash_link, flash_pipeline, late_chat_live,
                                run_flash, uploading)
from .helpers import A, B, live, later, sample_archive
from .test_drive_stage import ReplaySource, downloaded, step
from backend.archive import Archive
from backend.downloader import auto as auto_mod
from backend.downloader.base import DownloadResult


def ready_links(sent):
    """Each "全部处理完成"'s streamer and first link, in the order they went out."""
    return [(e.status.streamer, e.links[0].url) for e in sent if e.kind is EventKind.READY]


def test_one_flash_transfer_at_a_time_a_later_live_starts_once_the_earlier_is_uploaded(tmp_path):
    flash = FakeFlash(uploading(10), uploading(20), uploading(30), finished())
    ad, clock, sent = flash_pipeline(tmp_path, flash, ticked=(A, B))
    downloaded(ad, A)
    step(ad, "transcribe")
    downloaded(ad, B)
    step(ad, "transcribe")

    for _ in range(4):  # A's starts, and is looked at while it uploads
        if ad.next_due("flash") is None:
            clock.t += ad.sleep_time("flash")
        step(ad, "flash")
    assert [name for name, _ in flash.starts] == ["v"] and set(flash.looks) == {"fs-1"}
    assert ad.sleep_time("flash") > 1  # B's job does not keep the worker awake

    run_flash(ad, clock)
    assert len(flash.starts) == 2
    assert ready_links(sent) == [(A, flash_link(1)), (B, flash_link(2))]


def test_lives_that_waited_go_in_the_order_they_were_processed(tmp_path, monkeypatch):
    flash = FakeFlash()
    ad, clock, sent, _ = late_chat_live(tmp_path, flash, ticked=(A, B))
    step(ad, "flash")  # A waits for its chat
    clock.t += 30
    downloaded(ad, B)  # B, processed after A, is held by nothing
    step(ad, "transcribe")
    step(ad, "flash")
    step(ad, "flash")
    assert ready_links(sent) == [(B, flash_link(1))]

    chat_comes_in(ad, monkeypatch)
    run_flash(ad, clock)
    assert ready_links(sent) == [(B, flash_link(1)), (A, flash_link(2))]


def test_of_two_lives_due_together_the_one_processed_first_goes_first(tmp_path, monkeypatch):
    flash = FakeFlash()
    ad, clock, sent, _ = late_chat_live(tmp_path, flash, ticked=(A, B))
    step(ad, "flash")  # A waits for its chat: looked at again in a minute
    clock.t += 30
    downloaded(ad, B)
    step(ad, "transcribe")  # B's flash job is due at once, A's only in half a minute
    chat_comes_in(ad, monkeypatch)
    clock.t += 60  # the worker looks only now: both are due

    run_flash(ad, clock)
    assert ready_links(sent) == [(A, flash_link(1)), (B, flash_link(2))]


# ---- the archive is kept while its flash job is queued ----

def test_the_archive_stays_while_its_flash_job_is_queued_and_goes_by_the_usual_rule_after(tmp_path, monkeypatch):
    dates = iter(f"202611{d:02d}" for d in range(1, 31))

    def fake_download(url, settings, streamers, info=None, progress=None):
        folder = sample_archive(settings.output_dir, "A", next(dates), info.id, stem=info.id, video=b"x")
        return DownloadResult(info, folder, video=Archive.from_folder(folder).video)

    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: ReplaySource())
    monkeypatch.setattr(auto_mod, "download", fake_download)
    flash = FakeFlash(uploading())
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
    step(ad, "flash")  # started, uploading
    second = end_and_process("v2")
    assert first.exists() and second.exists()  # the first one's flash transfer is not uploaded yet

    flash.progress_answers = [finished()]
    run_flash(ad, clock)
    third = end_and_process("v3")
    assert not first.exists() and not second.exists() and third.exists()


# ---- only lives processed from now on ----

def test_archives_already_on_the_server_get_no_flash_job_when_the_service_starts(tmp_path):
    flash = FakeFlash()
    ad, clock, sent = flash_pipeline(tmp_path, flash)
    sample_archive(ad.settings.output_dir, "A", "20261101", "歌枠")

    async def a_while():
        worker = asyncio.create_task(ad.run())
        await asyncio.sleep(0.3)
        worker.cancel()

    asyncio.run(a_while())
    assert ad.load() == [] and flash.starts == [] and sent == []
