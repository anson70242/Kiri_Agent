"""A failed group file is not lost to a hiccup: a failed download or upload is one error of
its job, tried again after the automatic download's retry interval and given up on after its
maximum of errors; the local file stays between tries and goes when the job ends."""

from __future__ import annotations

import asyncio

from backend.drive import DriveFailed
from backend.group_files.jobs import GroupFileJobs

from .group_file_fakes import FOLDER, HOST, make

INTERVAL = 600.0


class Told:
    """A report that notes each failure as (file, reason, what happened)."""

    def __init__(self) -> None:
        self.failures: list[tuple[str, str, str]] = []
        self.places: list[str] = []

    async def uploaded(self, job, place) -> None:
        self.places.append(place)

    async def name_taken(self, job, place) -> None:
        pass

    async def failed(self, job, reason, failure) -> None:
        self.failures.append((job.name, reason, str(failure)))


def setup(tmp_path, **kw):
    told = Told()
    s = make(tmp_path, retry_interval=INTERVAL, max_errors=3, report=told, **kw)
    return s, told


def jobs(s):
    return GroupFileJobs(s.jobs_file).load()


def local_files(s):
    return [p for p in s.temp.rglob("*") if p.is_file()]


def test_a_failed_upload_is_one_error_tried_again_after_the_interval_and_the_file_kept(tmp_path):
    s, told = setup(tmp_path)
    s.drive.error = DriveFailed("网盘空间不足")
    s.post("【待校】第3话.ass", b"subtitle")
    s.settle()

    (job,) = jobs(s)
    assert (job.errors, job.last_error, job.next_try) == (1, "网盘空间不足", s.clock.t + INTERVAL)
    assert [p.read_bytes() for p in local_files(s)] == [b"subtitle"]
    assert told.failures == [("【待校】第3话.ass", "网盘空间不足", "first_error")]

    s.drive.error = None
    s.clock.t += INTERVAL - 1
    s.settle()  # not due yet
    assert s.drive.put == []

    s.clock.t += 1
    s.settle()
    assert s.drive.put == [(f"{FOLDER}/待校", "【待校】第3话.ass", b"subtitle")]
    assert jobs(s) == [] and local_files(s) == []


def test_a_job_is_given_up_on_at_its_maximum_of_errors_and_only_its_first_error_is_first(tmp_path):
    s, told = setup(tmp_path)
    s.drive.error = DriveFailed("网盘空间不足")
    s.post("【待校】第3话.ass")
    for _ in range(3):
        s.settle()
        s.clock.t += INTERVAL
    assert [f[2] for f in told.failures] == ["first_error", "later_error", "given_up"]
    assert jobs(s) == [] and local_files(s) == [] and s.drive.put == []


def test_a_failed_download_counts_like_a_failed_upload_and_is_tried_again(tmp_path):
    s, told = setup(tmp_path)
    url = f"{HOST}/ftn_handler/later"
    s.post("【熟肉】第3话.mp4", b"video", url=url)
    data = s.host.files.pop(url)  # the host answers 404 the first time
    s.settle()
    (job,) = jobs(s)
    assert job.errors == 1 and not job.downloaded and told.failures[0][2] == "first_error"

    s.host.files[url] = data
    s.clock.t += INTERVAL
    s.settle()
    assert [p[2] for p in s.drive.put] == [b"video"] and jobs(s) == []


def test_a_job_waiting_for_a_retry_does_not_hold_up_the_next(tmp_path):
    s, _ = setup(tmp_path)
    url = f"{HOST}/ftn_handler/later"
    s.post("【熟肉】第3话.mp4", b"video", url=url)
    del s.host.files[url]
    s.post("【已校】第4话.ass", b"next")
    s.settle()
    assert [p[1] for p in s.drive.put] == ["【已校】第4话.ass"]
    assert [j.name for j in jobs(s)] == ["【熟肉】第3话.mp4"]


def test_an_unexpected_exception_in_an_attempt_is_a_failed_attempt(tmp_path):
    s, told = setup(tmp_path)
    s.drive.error = RuntimeError("boom")
    s.post("【待校】第3话.ass")
    s.settle()
    assert told.failures == [("【待校】第3话.ass", "boom", "first_error")] and jobs(s)[0].errors == 1


def test_the_running_worker_tries_again_on_its_own(tmp_path):
    told = Told()
    s = make(tmp_path, retry_interval=0, max_errors=3, report=told)
    calls = []
    put = s.drive.put_file

    def flaky(local, folder, progress=None):
        calls.append(local.name)
        if len(calls) == 1:
            raise DriveFailed("网盘空间不足")
        return put(local, folder, progress)

    s.drive.put_file = flaky

    async def scenario() -> None:
        task = asyncio.create_task(s.keeper.run())
        await asyncio.sleep(0)
        s.post("【待校】第3话.ass", b"subtitle")
        for _ in range(300):
            if s.drive.put:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(calls) == 2 and s.drive.put and jobs(s) == []
