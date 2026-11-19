"""A lapsed authorization of the drive makes a group file wait for the owner: neither an
attempt nor an error, tried again at the usual interval, given up on 72 hours after the
first lapsed try; any try that is not lapsed ends the lapse."""

from __future__ import annotations

from backend.drive import DriveAuthorizationLapsed, DriveFailed
from backend.group_files.jobs import GroupFileJobs

from .group_file_fakes import FOLDER, make
from .test_group_file_retries import INTERVAL, Told

HOUR = 3600.0


def setup(tmp_path):
    told = Told()
    s = make(tmp_path, retry_interval=INTERVAL, max_errors=2, report=told)
    return s, told


def jobs(s):
    return GroupFileJobs(s.jobs_file).load()


def test_a_lapsed_try_is_no_error_and_the_job_waits_with_its_file(tmp_path):
    s, told = setup(tmp_path)
    s.drive.error = DriveAuthorizationLapsed("网盘授权已失效")
    s.post("【待校】第3话.ass", b"subtitle")
    for _ in range(5):  # more lapsed tries than the maximum of errors
        s.settle()
        s.clock.t += INTERVAL
    (job,) = jobs(s)
    assert job.errors == 0 and job.downloaded
    assert [f[2] for f in told.failures] == ["first_lapse"] + ["later_lapse"] * 4
    assert [p.read_bytes() for p in s.temp.rglob("*") if p.is_file()] == [b"subtitle"]

    s.drive.error = None  # the owner authorized the drive again
    s.settle()
    assert s.drive.put == [(f"{FOLDER}/待校", "【待校】第3话.ass", b"subtitle")] and jobs(s) == []


def test_a_job_is_given_up_on_72_hours_after_its_first_lapsed_try(tmp_path):
    s, told = setup(tmp_path)
    s.drive.error = DriveAuthorizationLapsed("网盘授权已失效")
    s.post("【待校】第3话.ass")
    s.settle()
    s.clock.t += 72 * HOUR - 1
    s.settle()
    assert len(jobs(s)) == 1

    s.clock.t += INTERVAL
    s.settle()
    assert jobs(s) == [] and [p for p in s.temp.rglob("*") if p.is_file()] == []
    name, reason, what = told.failures[-1]
    assert what == "given_up" and "72" in reason


def test_a_try_that_is_not_lapsed_ends_the_lapse(tmp_path):
    s, told = setup(tmp_path)
    s.drive.error = DriveAuthorizationLapsed("网盘授权已失效")
    s.post("【待校】第3话.ass")
    s.settle()
    s.clock.t += 70 * HOUR
    s.drive.error = DriveFailed("网盘空间不足")  # an error: the lapse is over
    s.settle()
    (job,) = jobs(s)
    assert job.errors == 1 and job.lapsed_since is None

    s.clock.t += INTERVAL
    s.drive.error = DriveAuthorizationLapsed("网盘授权已失效")  # a new lapse, with 72 hours of its own
    s.settle()
    s.clock.t += 71 * HOUR
    s.settle()
    assert len(jobs(s)) == 1
    assert [f[2] for f in told.failures] == ["first_lapse", "first_error", "first_lapse", "later_lapse"]
