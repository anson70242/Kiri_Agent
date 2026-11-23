"""The deploy check and flash transfers that wait for the flash account: it says how many wait,
and they never hold a deploy, started or not."""

from __future__ import annotations

from backend import check_state

from .helpers import B
from .test_check_state_flash import flash_job
from .test_drive_restart import queue


def waiting(job):
    job.flash_offline_since = job.flash_reminded_at = 150.0
    return job


def test_flash_transfers_waiting_for_the_account_are_counted_and_hold_no_deploy(tmp_path, capsys):
    cfg = queue(tmp_path, waiting(flash_job("v1", started=True)), waiting(flash_job("b1", s=B)), flash_job("v2"))

    assert check_state.main(["--config", str(cfg), "--require-idle"]) == 0
    assert "闪传：3 个任务，0 个正在切分影片，2 个在等闪传账号（只有正在切分的会挡住部署）。" in capsys.readouterr().out
