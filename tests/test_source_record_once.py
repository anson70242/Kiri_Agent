"""Each row of a live goes into a tab once: however often the module is told about a live
(a second message, a restart between the message and the save) and however many groups name
the tab, one 录播 row and one 网盘 row at most. A live is its streamer and stream id. Rows
are written one at a time, in the order told. Against a fake Tencent Docs endpoint and a
real groups store; made-up ids and links."""

from __future__ import annotations

import asyncio

from backend.monitor import Streamer
from backend.notify.qq import QQGroups, SourceRecordSheet
from backend.source_record_sheet import RowKind, SheetRow, SheetTab

from .source_record_fakes import FILE_ID, LINK, TAB_ID, FakeTencentDocs, make, record_sheet
from .test_source_record_processed import processed, store

SHEET = SourceRecordSheet(FILE_ID, TAB_ID)
OTHER = SourceRecordSheet("OTHERFILE2", "tab002")
RECORDING = ("G1", {"name": "向日葵字幕组", "streamers": ["A"], "source_record_sheet": SHEET})


def ours(tab) -> list[str]:
    """The link cells of the rows the module wrote (the typed rows start with 旧的直播)."""
    return [r[3] for r in tab.rows()[2:] if not r[3].startswith("旧的直播")]


def drive_row(**kw) -> SheetRow:
    return SheetRow(**{"kind": RowKind.DRIVE_COPY, "streamer": "A", "date": "20261122",
                       "title": "今日はのんびり雑談するよ〜", "link": "https://example.test/s/drive1", "stream_id": "v1", **kw})


def test_a_live_told_again_after_its_row_was_written_gets_no_second_row(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper)
    s.settle()
    processed(s.keeper)  # a second message for the same live
    s.settle()
    assert len(ours(s.tab)) == 1


def test_a_live_told_twice_before_its_row_is_written_gets_one_row(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper)
    processed(s.keeper)
    assert len(s.keeper.jobs.load()) == 1  # one job waits: its retries (if any) are the row's only ones
    s.settle()
    assert len(ours(s.tab)) == 1


def test_a_live_told_again_after_a_restart_gets_no_second_row(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper)
    s.settle()

    again = make(s.docs, groups=QQGroups(tmp_path / "qq_groups.json"))
    processed(again.keeper)  # the queue was saved before the restart, its message was not
    again.settle()
    assert len(ours(again.tab)) == 1


def test_a_stop_after_the_row_was_written_but_before_its_job_left_writes_no_second_row(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper)
    jobs_file = s.keeper.jobs.path
    waiting = jobs_file.read_bytes()
    s.settle()
    jobs_file.write_bytes(waiting)  # the service stopped before the written row's job was dropped

    again = make(s.docs, groups=QQGroups(tmp_path / "qq_groups.json"))
    again.settle()
    assert len(ours(again.tab)) == 1
    assert again.keeper.jobs.load() == []


def test_two_groups_naming_one_tab_get_one_row_also_when_told_twice(tmp_path):
    s = make(groups=store(tmp_path, RECORDING, ("G2", {"name": "另一个组", "streamers": ["A"], "source_record_sheet": SHEET})))
    processed(s.keeper)
    s.settle()
    processed(s.keeper)
    s.settle()
    assert len(ours(s.tab)) == 1


def test_two_lives_of_one_streamer_on_one_day_get_a_row_each(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper, stream_id="v1", title="同名")
    processed(s.keeper, stream_id="v2", title="同名")
    s.settle()
    assert len(ours(s.tab)) == 2


def test_two_streamers_with_one_stream_id_are_two_lives(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"streamers": ["A", "B"], "source_record_sheet": SHEET})))
    processed(s.keeper, Streamer("A", "youtube", "@a"), stream_id="same")
    processed(s.keeper, Streamer("B", "youtube", "@b"), stream_id="same")
    s.settle()
    assert [s.tab.text(r, 0) for r in (2, 3)] == ["B", "A"]


def test_the_archive_row_and_the_drive_row_of_one_live_are_both_written_once(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    tab = SheetTab(FILE_ID, TAB_ID)
    processed(s.keeper)
    s.keeper.add(tab, drive_row())
    s.settle()
    processed(s.keeper)
    s.keeper.add(tab, drive_row())
    s.settle()
    assert [s.tab.text(r, 4) for r in (2, 3, 4)] == ["网盘", "录播", "杂谈"]


def test_one_live_gets_its_row_in_each_tab_that_records_it(tmp_path):
    docs = FakeTencentDocs()
    first, second = record_sheet(docs), record_sheet(docs, OTHER.file_id, OTHER.tab_id)
    s = make(docs, groups=store(tmp_path, RECORDING))
    processed(s.keeper)
    s.settle()
    s.keeper.groups.update("G1", source_record_sheet=OTHER)  # the group names another tab now
    processed(s.keeper)
    s.settle()
    assert len(ours(first)) == len(ours(second)) == 1


def test_the_record_forgets_the_oldest_rows_first(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    s.keeper.record.cap = 2
    for live in ("v1", "v2", "v3"):
        processed(s.keeper, stream_id=live, title=live)
        s.settle()

    processed(s.keeper, stream_id="v3", title="v3")  # still remembered
    processed(s.keeper, stream_id="v1", title="v1")  # forgotten: written again
    s.settle()
    assert [c.split("（")[0] for c in ours(s.tab)] == ["v1", "v3", "v2", "v1"]


def test_rows_told_at_the_same_moment_are_all_written_in_the_order_told(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"streamers": ["A", "B"], "source_record_sheet": SHEET})))

    async def scenario() -> None:
        worker = asyncio.create_task(s.keeper.run())
        processed(s.keeper, Streamer("A", "youtube", "@a"), stream_id="a1")
        processed(s.keeper, Streamer("B", "youtube", "@b"), stream_id="b1")
        processed(s.keeper, Streamer("A", "youtube", "@a"), stream_id="a2")
        for _ in range(500):
            if len(ours(s.tab)) == 3 and not s.keeper.jobs.load():
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    assert [s.tab.text(r, 0) for r in (2, 3, 4, 5)] == ["A", "B", "A", "主播1"]
    assert [s.tab.merges_of(r) for r in (2, 3, 4)] == [s.tab.merges_of(5)] * 3


def test_a_link_is_kept_only_in_a_waiting_job(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper)
    assert LINK in s.keeper.jobs.path.read_text(encoding="utf-8")
    s.settle()
    for f in tmp_path.glob("source_record_*.json"):
        assert LINK not in f.read_text(encoding="utf-8"), f.name
