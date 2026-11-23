"""The deploy check and flash transfers: it reads the queue with the flash job's fields,
prints how many flash transfers there are and how many are being cut, holds a deploy only
while a video is being cut, and still reads a queue file written before flash transfers."""

from __future__ import annotations

import json

from backend import check_state
from backend.downloader.auto import Job

from .helpers import B, live
from .test_check_state import files
from .test_drive_restart import drive_job, queue

FLASH_FIELDS = ("transfer_id", "flash_link", "flash_expires", "flash_made", "cutting")


def flash_job(sid, s=None, started=False, cutting=False):
    """A flash job of the live `sid`: its flash transfer `started` (uploading in the flash
    service, or waiting for its next look), or not yet, its video being cut or not."""
    job = Job(live(s, sid=sid) if s else live(sid=sid), queued_at=100.0, next_try=200.0, stage="flash",
              video=f"/dl/{sid}.mp4", cutting=cutting)
    if started:
        job.transfer_id, job.flash_link, job.flash_expires, job.flash_made = (
            f"fs-{sid}", f"https://flash.example/s/fs-{sid}", 1_800_000_000.0, 1_700_000_000.0)
    return job


def test_flash_transfers_uploading_or_waiting_do_not_hold_a_deploy(tmp_path, capsys):
    cfg = queue(tmp_path, flash_job("v1", started=True), flash_job("v2"))
    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 0
    out = capsys.readouterr().out
    assert "下载队列：0 个任务" in out
    assert "闪传：2 个任务，0 个正在切分影片（只有正在切分的会挡住部署）" in out


def test_a_video_being_cut_holds_a_deploy_and_is_named(tmp_path, capsys):
    cfg = queue(tmp_path, flash_job("v1", started=True), flash_job("b1", s=B, cutting=True))
    assert check_state.main(["--config", str(cfg)]) == 0
    assert "闪传：2 个任务，1 个正在切分影片" in capsys.readouterr().out
    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 2
    out = capsys.readouterr().out
    assert "B title b1" in out and "等它切完再部署" in out
    assert "A title v1" not in out


def test_the_other_stages_hold_a_deploy_as_before_beside_flash_transfers(tmp_path, capsys):
    other = Job(live(sid="v3"), queued_at=100.0, next_try=200.0, stage="translate", video="/dl/v3.mp4")
    cfg = queue(tmp_path, flash_job("v1", started=True), drive_job("v2", uploading=True), other)
    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 2
    out = capsys.readouterr().out
    assert "下载队列：1 个任务" in out and "网盘上传：1 个任务，1 个正在上传" in out
    assert "闪传：1 个任务，0 个正在切分影片" in out
    assert "等处理完再部署" in out and "等它传完再部署" in out and "等它切完再部署" not in out


def test_a_queue_file_written_before_flash_transfers_is_read(tmp_path, capsys):
    old = [drive_job("v1").to_dict(), Job(live(sid="v2"), queued_at=100.0, next_try=200.0).to_dict()]
    for job in old:
        for name in FLASH_FIELDS:
            job.pop(name)
    cfg = queue(tmp_path)
    (tmp_path / "data" / "download_queue.json").write_text(json.dumps(old), encoding="utf-8")
    before = files(tmp_path / "data")

    assert check_state.main(["--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "状态文件正常" in out and "已损坏" not in out
    assert "下载队列：1 个任务" in out and "网盘上传：1 个任务，0 个正在上传" in out
    assert "闪传：0 个任务，0 个正在切分影片" in out
    assert files(tmp_path / "data") == before
