"""The drive stage across a restart or a deploy: the job marks in the queue file that its
upload is running, a mark left by a kill is cleared at start and the upload done again,
the deploy check waits only for a running upload, and a drive job that a service without
a drive finds is dropped with a word to the owner."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend import check_state
from backend.downloader.auto import Job
from backend.drive import DriveFailed
from backend.notify import EventKind

from .helpers import B, live
from .test_check_state import write_config
from .test_drive_failures import ScriptedDrive
from .test_drive_stage import DRIVE_LABEL, FakeDrive, kinds, links, make, processed, ready_as_today, rewrite_queue, step


# ---- a service without a drive finds a drive job ----

def test_a_drive_job_found_without_a_drive_is_dropped_with_ready_as_today_and_drive_upload_failed(tmp_path):
    processed(tmp_path, FakeDrive())
    restarted, _, sent = make(tmp_path, drive=None)  # the CLI was removed, then a restart
    step(restarted, "drive")

    assert restarted.load() == []
    assert kinds(sent) == [EventKind.READY, EventKind.DRIVE_FAILED]
    ready_as_today(sent)
    assert "不可用" in sent[1].detail and "取消" in sent[1].detail


def test_a_dropped_drive_job_whose_ready_went_out_only_tells_the_owner(tmp_path):
    ad, clock, _, _ = processed(tmp_path, ScriptedDrive(DriveFailed("网络超时")))
    step(ad, "drive")  # READY went out, the upload waits for a retry
    restarted, clock2, sent = make(tmp_path, drive=None)
    clock2.t = clock.t + 900
    step(restarted, "drive")

    assert restarted.load() == []
    assert kinds(sent) == [EventKind.DRIVE_FAILED]


# ---- the uploading mark ----

def run_until(ad, done, timeout=5.0):
    """Run the pipeline's workers (real time) until `done()` holds."""
    ad.clock = time.time

    async def scenario():
        worker = asyncio.create_task(ad.run())
        try:
            end = time.monotonic() + timeout
            while not done():
                assert time.monotonic() < end, "timed out"
                await asyncio.sleep(0.02)
        finally:
            worker.cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize("answer", [None, DriveFailed("网络超时")], ids=["uploaded", "failed"])
def test_the_job_is_marked_uploading_in_the_queue_file_while_its_upload_runs(tmp_path, answer):
    drive = FakeDrive(error=answer)
    ad, _, sent, _ = processed(tmp_path, drive)
    marks = []
    drive.during = lambda progress: marks.append([j.uploading for j in ad.load()])
    run_until(ad, lambda: EventKind.READY in kinds(sent))

    assert marks == [[True]]
    assert [j.uploading for j in ad.load()] == ([] if answer is None else [False])


def killed_while_uploading(tmp_path, **job):
    """A's drive job, left in the queue file by a service killed during its upload."""
    processed(tmp_path, FakeDrive())
    rewrite_queue(tmp_path, lambda j: j.update(uploading=True, **job))


def test_a_mark_left_by_a_kill_is_cleared_when_the_service_starts(tmp_path):
    killed_while_uploading(tmp_path, next_try=time.time() + 3600)  # not due for a while
    ad, _, _ = make(tmp_path, FakeDrive())
    run_until(ad, lambda: not ad.load()[0].uploading)


def test_an_upload_a_kill_interrupted_is_done_again_and_ready_still_goes_out_once(tmp_path):
    killed_while_uploading(tmp_path)
    drive = FakeDrive()
    ad, _, sent = make(tmp_path, drive)
    run_until(ad, lambda: not ad.load())

    assert len(drive.calls) == 1
    assert kinds(sent) == [EventKind.READY]
    assert links(sent[0])[0] == ("https://drive.example/s/abc?pwd=x1y2", DRIVE_LABEL)


# ---- the deploy check ----

def queue(tmp_path, *jobs):
    """The config of the check-state tests, with these jobs in its download queue."""
    cfg = write_config(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "download_queue.json").write_text(json.dumps([j.to_dict() for j in jobs]), encoding="utf-8")
    return cfg


def drive_job(sid, uploading=False, s=None):
    status = live(s, sid=sid) if s else live(sid=sid)
    return Job(status, queued_at=100.0, next_try=200.0, stage="drive", video=f"/dl/{sid}.mp4", uploading=uploading)


def test_drive_jobs_that_only_wait_do_not_hold_a_deploy(tmp_path, capsys):
    cfg = queue(tmp_path, drive_job("v1"), drive_job("v2"))
    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 0
    out = capsys.readouterr().out
    assert "下载队列：0 个任务" in out and "网盘上传：2 个任务，0 个正在上传" in out


def test_a_running_upload_holds_a_deploy_and_is_named(tmp_path, capsys):
    cfg = queue(tmp_path, drive_job("v1"), drive_job("b1", uploading=True, s=B))
    assert check_state.main(["--config", str(cfg)]) == 0
    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 2
    out = capsys.readouterr().out
    assert "网盘上传：2 个任务，1 个正在上传" in out
    assert "B title b1" in out and "等它传完再部署" in out


def test_jobs_of_the_other_stages_hold_a_deploy_as_before_beside_waiting_drive_jobs(tmp_path, capsys):
    other = Job(live(sid="v3"), queued_at=100.0, next_try=200.0, stage="translate", video="/dl/v3.mp4")
    cfg = queue(tmp_path, drive_job("v1"), other)
    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 2
    out = capsys.readouterr().out
    assert "下载队列：1 个任务" in out and "等处理完再部署" in out
