"""A tagged group file lands in its folder on the drive: taken in from the gateway, kept as
a job, downloaded from QQ's host, uploaded under its name, its local copy deleted. Through
the group-files module with a fake host, a fake drive and a fake clock."""

from __future__ import annotations

import logging

import pytest

from backend.drive import DriveFailed
from backend.group_files.jobs import GroupFileJobs

from .group_file_fakes import FOLDER, GROUP, make


@pytest.mark.parametrize("name, subfolder", [
    ("【待轴】第3话.mp4", "剪辑"), ("【空轴】第3话.ass", "空轴"), ("【待校】第3话.ass", "待校"),
    ("【已校】第3话.ass", "已校"), ("【熟肉】第3话.mp4", "熟肉")])
def test_a_tagged_file_lands_in_its_folder_under_its_name_and_its_local_copy_goes(tmp_path, name, subfolder):
    s = make(tmp_path)
    s.post(name, b"the file")
    s.settle()

    assert s.drive.put == [(f"{FOLDER}/{subfolder}", name, b"the file")]
    assert GroupFileJobs(s.jobs_file).load() == []
    assert not s.temp.exists() or list(s.temp.rglob("*")) == []


def test_a_posted_file_is_a_saved_job_at_once(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3话.ass", b"abc", msg_id="MSG7")

    (job,) = GroupFileJobs(s.jobs_file).load()
    assert (job.group, job.name, job.size, job.msg_id, job.subfolder, job.group_folder) == (
        GROUP, "【待校】第3话.ass", 3, "MSG7", "待校", FOLDER)
    assert job.posted_at == 1795038060.0  # 2026-11-19 05:41 UTC+8


@pytest.mark.parametrize("name", ["第3话.ass", "【待校】第3话.mp4", "notes.txt"])
def test_a_file_no_rule_keeps_is_ignored(tmp_path, name):
    s = make(tmp_path)
    s.post(name)
    s.settle()
    assert GroupFileJobs(s.jobs_file).load() == [] and s.host.requests == [] and s.drive.put == []


def test_a_file_in_a_group_whose_switch_is_off_or_that_is_unknown_is_ignored(tmp_path):
    s = make(tmp_path, to_drive=False)
    s.post("【待校】第3话.ass")
    s.post("【待校】第3话.ass", group="NEVER_REGISTERED")
    s.groups.update(GROUP, status="removed", group_files_to_drive=True)
    s.post("【待校】第3话.ass")
    s.settle()
    assert GroupFileJobs(s.jobs_file).load() == [] and s.host.requests == [] and s.drive.put == []


def test_without_the_drive_files_are_ignored_with_one_warning(tmp_path, caplog):
    s = make(tmp_path, no_drive=True)
    with caplog.at_level(logging.WARNING, logger="backend.group_files"):
        s.post("【待校】第3话.ass")
        s.post("【已校】第3话.ass")
    s.settle()
    assert GroupFileJobs(s.jobs_file).load() == [] and s.host.requests == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_files_go_one_at_a_time_in_the_order_posted(tmp_path):
    s = make(tmp_path)
    s.post("【熟肉】第1话.mp4", b"one", msg_id="M1")
    s.post("【待校】第2话.ass", b"two", msg_id="M2")
    s.post("【空轴】第3话.ass", b"three", msg_id="M3")
    s.settle()
    assert [p[1] for p in s.drive.put] == ["【熟肉】第1话.mp4", "【待校】第2话.ass", "【空轴】第3话.ass"]


def test_a_name_with_characters_a_file_name_cannot_have_is_uploaded_with_them_replaced(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3/4话.ass")
    s.settle()
    assert [p[1] for p in s.drive.put] == ["【待校】第3_4话.ass"]


def test_a_name_already_in_the_folder_ends_the_job_with_a_log_line_and_nothing_uploaded(tmp_path, caplog):
    s = make(tmp_path)
    s.drive.folders[f"{FOLDER}/待校"] = {"【待校】第3话.ass": b"older"}
    with caplog.at_level(logging.INFO, logger="backend.group_files"):
        s.post("【待校】第3话.ass", b"newer")
        s.settle()
    assert s.drive.put == [] and s.drive.folders[f"{FOLDER}/待校"]["【待校】第3话.ass"] == b"older"
    assert GroupFileJobs(s.jobs_file).load() == [] and list(s.temp.rglob("*")) == []
    assert any("【待校】第3话.ass" in r.message for r in caplog.records)


def test_a_failed_upload_is_logged_and_the_job_and_its_file_dropped(tmp_path, caplog):
    s = make(tmp_path)
    s.drive.error = DriveFailed("网盘空间不足")
    with caplog.at_level(logging.WARNING, logger="backend.group_files"):
        s.post("【待校】第3话.ass")
        s.settle()
    assert GroupFileJobs(s.jobs_file).load() == [] and list(s.temp.rglob("*")) == []
    assert any("网盘空间不足" in r.message for r in caplog.records)

    s.drive.error = None  # the worker goes on with the next file
    s.post("【已校】第3话.ass")
    s.settle()
    assert [p[1] for p in s.drive.put] == ["【已校】第3话.ass"]


def test_an_unexpected_error_in_an_attempt_drops_the_job_and_the_worker_goes_on(tmp_path):
    s = make(tmp_path)
    s.drive.error = RuntimeError("boom")
    s.post("【待校】第3话.ass")
    s.settle()
    assert GroupFileJobs(s.jobs_file).load() == []
    s.drive.error = None
    s.post("【已校】第3话.ass")
    s.settle()
    assert len(s.drive.put) == 1
