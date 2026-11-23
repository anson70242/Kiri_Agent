"""What goes on beside a flash transfer that waits for the flash account: the drive copy is
uploaded and told as usual, the archive is kept from a prune, the thirty minutes of a hung upload
do not run out while the account is offline nor right after the scan, and a waiting job is never
marked as being cut for the deploy check. Through the pipeline with a fake clock (real time for
the busy mark), a fake drive and a fake flash service whose account is offline for a while."""

from __future__ import annotations

import time

from backend.archive import Archive
from backend.downloader import auto as auto_mod
from backend.downloader.base import DownloadResult
from backend.notify import EventKind, StreamEvent

from .flash_stage_fakes import (HOUR, AccountFlash, alerts_of, flash_pipeline, pass_time, processed_live, stages,
                                uploading)
from .helpers import A, later, live, sample_archive
from .test_drive_stage import DRIVE_LINK, FakeDrive, ReplaySource, kinds, links, rewrite_queue, step
from .test_drive_restart import run_until
from .test_flash_restart_cutting import marks, video_over_the_limit


def test_the_drive_copy_of_a_waiting_live_is_uploaded_and_told_as_usual(tmp_path):
    ad, clock, sent, _ = processed_live(tmp_path, AccountFlash(), folders={"youtube:@a": "录播/A"},
                                        drive=FakeDrive())
    alerts_of(ad)
    step(ad, "flash")  # waits for the account
    step(ad, "drive")

    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.DRIVE_UPLOADED]
    assert links(sent[-1]) == [DRIVE_LINK]
    assert stages(ad) == ["flash"]


def test_the_archive_of_a_waiting_live_is_kept_from_a_prune_until_it_stops_waiting(tmp_path, monkeypatch):
    dates = iter(f"202611{d:02d}" for d in range(1, 31))

    def fake_download(url, settings, streamers, info=None, progress=None):
        folder = sample_archive(settings.output_dir, "A", next(dates), info.id, stem=info.id, video=b"x")
        return DownloadResult(info, folder, video=Archive.from_folder(folder).video)

    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: ReplaySource())
    monkeypatch.setattr(auto_mod, "download", fake_download)
    ad, clock, _ = flash_pipeline(tmp_path, AccountFlash())
    alerts_of(ad)
    ad.keep = {A.id: 1}

    def end_and_process(sid):
        ad.on_ended(StreamEvent(EventKind.OFFLINE, live(A, sid=sid), later(60)))
        job = ad.next_due("download")
        outcome = ad.attempt(job)
        ad.finish(job, outcome)
        step(ad, "transcribe")
        return outcome.result.output_dir

    first = end_and_process("v1")
    step(ad, "flash")  # waits for the account
    pass_time(ad, clock, HOUR)
    second = end_and_process("v2")
    assert first.exists() and second.exists()

    pass_time(ad, clock, 72 * HOUR)  # the first live stops waiting
    assert "v1" not in [j.status.stream_id for j in ad.load()]
    end_and_process("v3")
    assert not first.exists() and second.exists()  # the second still waits


def test_a_hung_uploads_thirty_minutes_do_not_run_out_while_waiting_nor_right_after_the_scan(tmp_path):
    flash = AccountFlash(uploading(50), state="online")
    ad, clock, _, _ = processed_live(tmp_path, flash, full=True)
    alerts_of(ad)
    step(ad, "flash")  # started
    step(ad, "flash")  # 50 bytes sent
    flash.state = "offline"
    pass_time(ad, clock, 10)
    pass_time(ad, clock, 2 * HOUR)

    flash.state = "online"  # the upload goes on where it stopped: still 50 bytes sent
    pass_time(ad, clock, 60)
    (job,) = ad.load()
    assert job.errors == 0

    pass_time(ad, clock, 29 * 60)
    assert ad.load()[0].errors == 0
    pass_time(ad, clock, 60)
    assert ad.load()[0].errors == 1  # thirty minutes without a byte since the scan: a failed try


def test_a_waiting_job_is_not_marked_as_being_cut_only_its_try_after_the_scan_is(tmp_path, monkeypatch):
    flash = AccountFlash(uploading())
    ad, _, _, _ = processed_live(tmp_path, flash, full=True)
    alerts_of(ad)
    ad.clock = time.time  # the workers below run in real time
    step(ad, "flash")  # waits for the account
    rewrite_queue(tmp_path, lambda job: job.update(next_try=0))  # its minute is over
    seen: list[tuple[str, list[bool]]] = []
    video_over_the_limit(monkeypatch, lambda: seen.append(("cutting", marks(ad))))
    asking = flash.require_online

    def looking():
        seen.append(("account", marks(ad)))
        return asking()

    flash.require_online = looking
    run_until(ad, lambda: flash.accounts >= 2)  # a look while it waits
    assert seen == [("account", [False])]

    flash.state = "online"
    rewrite_queue(tmp_path, lambda job: job.update(next_try=0))  # its minute is over
    run_until(ad, lambda: len(flash.looks) >= 1)
    assert seen == [("account", [False]),
                    ("account", [False]),  # the look that finds the account back
                    ("account", [True]), ("cutting", [True])]  # the try that cuts
