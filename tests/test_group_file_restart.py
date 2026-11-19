"""A restart loses no group file: the jobs file is runtime state; a job not downloaded yet is
downloaded, one downloaded is uploaded from its local file, an upload mark left by a kill is
cleared and that upload done again; temporary files of no job are removed. A damaged jobs
file is set aside, the owner alerted, and the service goes on empty. And a job whose group
was switched off meanwhile is cancelled before it starts."""

from __future__ import annotations

import asyncio

from backend.drive import DriveFailed
from backend.group_files import GroupFileKeeper
from backend.group_files.jobs import GroupFileJobs
from backend.state_file import set_incident_hook

from .group_file_fakes import FOLDER, GROUP, make
from .test_group_file_retries import INTERVAL, Told


def jobs(s):
    return GroupFileJobs(s.jobs_file).load()


def files(s):
    return sorted(p.relative_to(s.temp).as_posix() for p in s.temp.rglob("*") if p.is_file())


def restart(s, until):
    """A new keeper on the same files (the service started again), run until `until()`."""
    keeper = GroupFileKeeper(s.groups, s.drive, jobs_file=s.jobs_file, temp=s.temp,
                             client=s.host.client(), clock=s.clock)

    async def scenario() -> None:
        task = asyncio.create_task(keeper.run())
        for _ in range(300):
            await asyncio.sleep(0.01)
            if until():
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    return keeper


def test_the_upload_is_marked_as_running_while_it_runs_and_cleared_when_it_fails(tmp_path):
    s = make(tmp_path, retry_interval=INTERVAL, report=Told())
    seen = []

    def put(local, folder, progress=None):
        seen.append([j.uploading for j in jobs(s)])
        raise DriveFailed("网盘空间不足")

    s.drive.put_file = put
    s.post("【待校】第3话.ass")
    asyncio.run(s.keeper.download_next())
    assert [j.uploading for j in jobs(s)] == [False]
    asyncio.run(s.keeper.upload_next())
    assert seen == [[True]] and [j.uploading for j in jobs(s)] == [False]


def test_after_a_restart_a_job_not_downloaded_yet_is_downloaded_and_uploaded(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3话.ass", b"subtitle")  # the service stopped before the download

    restart(s, until=lambda: s.drive.put)
    assert s.drive.put == [(f"{FOLDER}/待校", "【待校】第3话.ass", b"subtitle")]
    assert jobs(s) == [] and files(s) == []


def test_a_mark_left_by_a_kill_is_cleared_and_the_upload_done_again_from_the_local_file(tmp_path):
    s = make(tmp_path)
    url = s.post("【待校】第3话.ass", b"subtitle")
    asyncio.run(s.keeper.download_next())
    (job,) = jobs(s)
    job.uploading = True  # killed while uploading
    GroupFileJobs(s.jobs_file).replace(job)
    del s.host.files[url]  # QQ's address is no longer needed

    restart(s, until=lambda: s.drive.put)
    assert s.drive.put == [(f"{FOLDER}/待校", "【待校】第3话.ass", b"subtitle")]
    assert jobs(s) == [] and files(s) == []


def test_a_mark_left_by_a_kill_is_cleared_at_start_even_when_the_job_is_not_due(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3话.ass")
    asyncio.run(s.keeper.download_next())
    (job,) = jobs(s)
    job.uploading, job.next_try = True, s.clock.t + INTERVAL  # a deploy check would wait for it
    GroupFileJobs(s.jobs_file).replace(job)

    restart(s, until=lambda: not any(j.uploading for j in jobs(s)))
    assert [j.uploading for j in jobs(s)] == [False] and s.drive.put == []


def test_an_upload_tried_again_takes_the_local_file_under_the_name_the_last_try_gave_it(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3话.ass", b"subtitle")
    asyncio.run(s.keeper.download_next())
    (job,) = jobs(s)
    (local,) = (s.temp / job.id).iterdir()
    local.rename(local.with_name("【待校】第3话.2026-11-19-0541.ass"))  # named for the drive, then failed

    s.settle()
    assert [p[2] for p in s.drive.put] == [b"subtitle"] and jobs(s) == []


def test_temporary_files_that_belong_to_no_job_are_removed_at_start(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3话.ass", b"subtitle")
    asyncio.run(s.keeper.download_next())
    (job,) = jobs(s)
    for j in jobs(s):  # the job waits for a retry: kept as it is
        j.next_try = s.clock.t + INTERVAL
        GroupFileJobs(s.jobs_file).replace(j)
    (s.temp / "0123456789ab").mkdir()
    (s.temp / "0123456789ab" / "【熟肉】第1话.mp4.part").write_bytes(b"half")
    (s.temp / "stray.tmp").write_bytes(b"x")

    restart(s, until=lambda: not (s.temp / "0123456789ab").exists() and not (s.temp / "stray.tmp").exists())
    assert files(s) == [f"{job.id}/【待校】第3话.ass"]


def test_a_downloaded_job_whose_local_file_is_gone_is_downloaded_again(tmp_path):
    s = make(tmp_path)
    s.post("【待校】第3话.ass", b"subtitle")
    asyncio.run(s.keeper.download_next())
    (job,) = jobs(s)
    for p in (s.temp / job.id).iterdir():
        p.unlink()

    restart(s, until=lambda: s.drive.put)
    assert [p[2] for p in s.drive.put] == [b"subtitle"]


def test_a_damaged_jobs_file_is_set_aside_the_owner_alerted_and_the_service_goes_on_empty(tmp_path):
    alerts = []
    set_incident_hook(alerts.append)
    try:
        s = make(tmp_path)
        s.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        s.jobs_file.write_text("{not json", encoding="utf-8")
        assert asyncio.run(s.keeper.download_next()) is False
        s.post("【待校】第3话.ass")
        s.settle()
    finally:
        set_incident_hook(None)
    assert len(alerts) == 1 and alerts[0].path == s.jobs_file
    assert list(s.jobs_file.parent.glob("group_file_jobs.json.corrupt-*"))
    assert len(s.drive.put) == 1


def test_a_job_of_a_group_switched_off_before_its_download_is_cancelled_without_an_error(tmp_path):
    told = Told()
    s = make(tmp_path, report=told)
    s.post("【待校】第3话.ass")
    s.groups.update(GROUP, group_files_to_drive=False)
    s.settle()
    assert s.host.requests == [] and s.drive.put == [] and jobs(s) == [] and files(s) == []
    assert told.failures == []


def test_a_job_of_a_group_switched_off_before_its_upload_is_cancelled_and_its_file_deleted(tmp_path):
    told = Told()
    s = make(tmp_path, report=told)
    s.post("【待校】第3话.ass")
    asyncio.run(s.keeper.download_next())
    assert files(s)
    s.groups.update(GROUP, group_files_to_drive=False)
    s.settle()
    assert s.drive.put == [] and jobs(s) == [] and files(s) == [] and told.failures == []


def test_an_upload_already_running_when_the_group_is_switched_off_finishes(tmp_path):
    s = make(tmp_path)
    put = s.drive.put_file

    def switched_off_meanwhile(local, folder, progress=None):
        s.groups.update(GROUP, group_files_to_drive=False)
        return put(local, folder, progress)

    s.drive.put_file = switched_off_meanwhile
    s.post("【待校】第3话.ass", b"subtitle")
    s.settle()
    assert [p[2] for p in s.drive.put] == [b"subtitle"] and jobs(s) == []
