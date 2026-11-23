"""The flash stage across a restart: a flash transfer that was started goes on by its id
(asked for its progress, never started or prepared a second time), a flash job killed
before its flash transfer was started starts from the beginning, and the staging folders a
kill left are removed when the service starts. A restart is the pipeline built again over
the same queue file and downloads folder; a queue file written before flash transfers starts
the service as before."""

from __future__ import annotations

from backend.archive import Downloads
from backend.notify import EventKind

from .flash_stage_fakes import (FLASH_JOB_FIELDS, FakeFlash, finished, flash_pipeline, flash_ready_links,
                                processed_live, ready, run_flash, staging, uploading)
from .test_drive_restart import run_until
from .test_drive_stage import FakeDrive, kinds, links, rewrite_queue, step


def started_and_uploading(tmp_path):
    """A's live whose flash transfer was started and is uploading, its subtitles and sheet
    in its staging folder, when the service stops. Returns the clock."""
    ad, clock, _, _ = processed_live(tmp_path, FakeFlash(uploading()), full=True)
    step(ad, "flash")  # started
    step(ad, "flash")  # uploading
    return clock


# ---- a started flash transfer goes on by its id ----

def test_a_flash_job_with_an_id_only_asks_for_its_progress_after_a_restart(tmp_path):
    clock = started_and_uploading(tmp_path)
    kept = staging(tmp_path) / "note.txt"
    kept.write_text("the flash service still reads this folder", encoding="utf-8")

    after = FakeFlash(uploading(70))
    restarted, clock2, sent = flash_pipeline(tmp_path, after)
    clock2.t = clock.t + 60
    step(restarted, "flash")

    assert after.starts == [] and after.looks == ["fs-1"]
    assert kept.exists()  # nothing prepared again: preparing empties the staging folder first
    assert kinds(sent) == []


def test_a_flash_transfer_that_finished_while_the_service_was_down_is_ready_at_the_first_look_once(tmp_path):
    clock = started_and_uploading(tmp_path)

    after = FakeFlash(finished())
    restarted, clock2, sent = flash_pipeline(tmp_path, after)
    clock2.t = clock.t + 3600
    run_flash(restarted, clock2)

    assert after.starts == [] and after.looks == ["fs-1"]
    assert kinds(sent) == [EventKind.READY]
    assert links(ready(sent)) == flash_ready_links()
    assert not staging(tmp_path).exists()


# ---- the staging folders at start ----

def test_leftover_staging_folders_are_removed_at_start_but_that_of_a_flash_transfer_still_uploading(tmp_path):
    started_and_uploading(tmp_path)
    uploading_files = sorted(p.name for p in staging(tmp_path).iterdir())
    leftover = Downloads(tmp_path / "dl").flash_staging / "B" / "20261101" / "雑談"
    leftover.mkdir(parents=True)
    (leftover / "v (1／2 00.00.00–01.00.00).mp4").write_bytes(b"half a part")

    # The upload has moved on meanwhile: `run_until` runs on the real clock, far from the
    # tests' clock the job was saved with, and an upload that had not moved would count as hung.
    restarted, _, _ = flash_pipeline(tmp_path, FakeFlash(uploading(70)))
    run_until(restarted, lambda: not leftover.exists())

    assert not (Downloads(tmp_path / "dl").flash_staging / "B").exists()
    assert sorted(p.name for p in staging(tmp_path).iterdir()) == uploading_files


def test_a_flash_job_killed_while_its_video_was_cut_starts_from_the_beginning(tmp_path):
    processed_live(tmp_path, full=True)  # its flash job is next, no flash transfer started
    part = staging(tmp_path) / "v (1／2 00.00.00–01.00.00).mp4"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"half a part")  # what the kill left of the cutting
    rewrite_queue(tmp_path, lambda job: job.update(cutting=True))

    after = FakeFlash()
    restarted, _, sent = flash_pipeline(tmp_path, after)
    run_until(restarted, lambda: not restarted.load())

    assert len(after.starts) == 1 and after.existed == [True]
    assert part.name not in after.starts[0][1]
    assert kinds(sent) == [EventKind.READY]
    assert links(ready(sent)) == flash_ready_links()


def test_a_cutting_mark_left_by_a_kill_is_cleared_when_the_service_starts(tmp_path):
    processed_live(tmp_path)
    rewrite_queue(tmp_path, lambda job: job.update(cutting=True, next_try=job["next_try"] + 10**10))

    restarted, _, _ = flash_pipeline(tmp_path)
    run_until(restarted, lambda: not restarted.load()[0].cutting)


def test_the_service_starts_on_a_queue_file_written_before_flash_transfers(tmp_path):
    processed_live(tmp_path, None, folders={"youtube:@a": "录播/A"}, drive=FakeDrive())  # a drive job
    rewrite_queue(tmp_path, lambda job: [job.pop(name) for name in FLASH_JOB_FIELDS])

    restarted, _, sent = flash_pipeline(tmp_path, folders={"youtube:@a": "录播/A"}, drive=FakeDrive())
    run_until(restarted, lambda: not restarted.load())

    assert kinds(sent) == [EventKind.DRIVE_UPLOADED]
