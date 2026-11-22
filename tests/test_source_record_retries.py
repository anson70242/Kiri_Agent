"""A row that cannot be written is tried again by itself, with the pipeline's retry interval
and maximum of errors, and given up on after the last; the job waits in the jobs file, also
across a restart. Against a fake Tencent Docs endpoint and a fake clock; made-up ids and links."""

from __future__ import annotations

import asyncio

from backend.notify.qq import QQGroups, SourceRecordSheet
from backend.source_record_sheet import RowJobs

from .source_record_fakes import FILE_ID, TAB_ID, make
from .test_source_record_processed import processed, store

INTERVAL = 900.0
SHEET = SourceRecordSheet(FILE_ID, TAB_ID)
RECORDING = ("G1", {"name": "章鱼烧字幕组", "streamers": ["A"], "source_record_sheet": SHEET})


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def setup(tmp_path, docs=None, clock=None, alerts=None, max_errors=3):
    alerts = [] if alerts is None else alerts
    clock = clock or Clock()
    s = make(docs, groups=store(tmp_path, RECORDING) if docs is None else QQGroups(tmp_path / "qq_groups.json"),
             clock=clock, retry_interval=INTERVAL, max_errors=max_errors, alert=alerts.append)
    return s, clock, alerts


def waiting(tmp_path):
    return RowJobs(tmp_path / "source_record_jobs.json").load()


def ours(s):
    """The rows the module wrote: their link cell's text."""
    return [s.tab.text(r, 6) for r in range(s.tab.row_count()) if s.tab.text(r, 6).endswith("全部下载")]


def test_a_failed_try_waits_for_the_retry_interval_and_no_sooner(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.fail("sheet.get_cell_data")
    processed(s.keeper)
    s.settle()

    (job,) = waiting(tmp_path)
    assert (job.errors, job.next_try) == (1, clock.t + INTERVAL)
    assert ours(s) == []

    clock.t += INTERVAL - 1
    s.settle()
    assert ours(s) == []

    clock.t += 1
    s.settle()
    assert len(ours(s)) == 1 and waiting(tmp_path) == []
    assert alerts == []  # got through on a later try: nothing is told


def test_the_wait_survives_a_restart(tmp_path):
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.get_cell_data")
    processed(s.keeper)
    s.settle()

    again, _, alerts = setup(tmp_path, s.docs, clock)  # the service started again on the same files
    again.settle()
    assert ours(again) == [] and waiting(tmp_path)[0].errors == 1

    clock.t += INTERVAL
    again.settle()
    assert len(ours(again)) == 1 and waiting(tmp_path) == [] and alerts == []


def test_after_the_last_failed_try_the_job_is_dropped_and_the_owner_told_once(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.fail("sheet.get_cell_data", times=10)
    processed(s.keeper)
    for _ in range(5):
        s.settle()
        clock.t += INTERVAL
    assert len(s.docs.tools()) == 3  # three tries, one call each
    assert waiting(tmp_path) == [] and ours(s) == []
    assert len(alerts) == 1
    assert alerts == ["\n".join([
        "QQ 群「章鱼烧字幕组」的录播源记录表没写进这一行，已放弃，请手动补上",
        "配信者：A",
        "日期：2026/11/22",
        "直播：戦場がマイホームの声優です",
        "项目类型：录播",
        "原因：Tencent Docs sheet.get_cell_data failed: code:400007, msg:internal error",
    ])]


def test_a_job_waiting_for_its_retry_does_not_hold_up_the_next(tmp_path):
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.get_cell_data")
    processed(s.keeper, title="第一场", stream_id="v1")
    processed(s.keeper, title="第二场", stream_id="v2")
    s.settle()
    assert [t.split("（")[0] for t in ours(s)] == ["第二场"]
    assert [j.row.title for j in waiting(tmp_path)] == ["第一场"]


def test_the_running_worker_tries_again_on_its_own_when_the_retry_is_due(tmp_path):
    s = make(groups=store(tmp_path, RECORDING), retry_interval=0.2, max_errors=3)
    s.docs.fail("sheet.get_cell_data")

    async def scenario() -> None:
        task = asyncio.create_task(s.keeper.run())
        await asyncio.sleep(0)
        processed(s.keeper)
        for _ in range(500):
            if ours(s):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(ours(s)) == 1 and waiting(tmp_path) == []


def test_telling_never_waits_for_a_failing_sheet(tmp_path):
    # The pipeline's telling ("全部处理完成", "网盘上传完成") saves the job and returns: no
    # Tencent Docs call is made in it, failing or not, and a job waiting for its retry is
    # no reason for a second telling to wait either.
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.get_cell_data", times=10)
    processed(s.keeper, stream_id="v1")
    assert s.docs.calls == []
    s.settle()
    calls = len(s.docs.calls)
    processed(s.keeper, stream_id="v2")
    assert len(s.docs.calls) == calls and len(waiting(tmp_path)) == 2
