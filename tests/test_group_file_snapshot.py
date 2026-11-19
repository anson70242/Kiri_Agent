"""The group-files module's snapshot, for the queue box: the job being downloaded or
uploaded with its progress, and the jobs waiting."""

from __future__ import annotations

import asyncio

from .group_file_fakes import FOLDER, make


def test_without_jobs_the_snapshot_is_empty(tmp_path):
    assert make(tmp_path).keeper.snapshot() == {"active": [], "queue": []}


def test_waiting_jobs_are_listed_oldest_first_with_what_they_wait_for(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第1话.ass", b"one")
    s.post("【已校】第2话.ass", b"two")
    asyncio.run(s.keeper.download_next())

    snap = s.keeper.snapshot()
    assert snap["active"] == []
    assert [(r["group"], r["file"], r["waiting_for"]) for r in snap["queue"]] == [
        (FOLDER, "【待校】第1话.ass", "upload"), (FOLDER, "【已校】第2话.ass", "download")]


def test_the_upload_running_is_active_with_its_progress(tmp_path):
    s = make(tmp_path)
    s.post("【熟肉】第1话.mp4", b"x" * 10)
    s.post("【已校】第2话.ass", b"two")
    asyncio.run(s.keeper.download_next())
    seen = []
    put = s.drive.put_file

    def watched(local, folder, progress=None):
        progress(4, 10)
        seen.append(s.keeper.snapshot())
        return put(local, folder, progress)

    s.drive.put_file = watched
    asyncio.run(s.keeper.upload_next())

    (snap,) = seen
    (row,) = snap["active"]
    assert (row["group"], row["file"], row["doing"], row["done"], row["total"]) == (
        FOLDER, "【熟肉】第1话.mp4", "upload", 4, 10)
    assert [r["file"] for r in snap["queue"]] == ["【已校】第2话.ass"]
