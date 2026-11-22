"""A tab's rows go in the order told: a job waiting for its retry holds back the later jobs of
its own tab (and only of its tab), until it is written or given up on, also across a restart.
So a try that left an empty third row finds it again on its retry: no later row of the same
tab was put above it. Against a fake Tencent Docs endpoint and a fake clock; made-up ids and
links."""

from __future__ import annotations

from backend.notify.qq import SourceRecordSheet

from .source_record_fakes import FILE_ID, SPANS, TAB_ID, FakeTencentDocs, record_sheet
from .test_source_record_processed import B, processed, store
from .test_source_record_retries import INTERVAL, setup, waiting

SHEET = SourceRecordSheet(FILE_ID, TAB_ID)
OTHER = SourceRecordSheet("OTHERFILE2", "tab002")


def titles(tab) -> list[str]:
    """The titles of the rows the module wrote, top down."""
    return [tab.text(r, 6).split("（")[0] for r in range(tab.row_count()) if tab.text(r, 6).endswith("全部下载")]


def test_a_later_row_of_the_same_tab_waits_for_the_retry_and_goes_above_it(tmp_path):
    s, clock, alerts = setup(tmp_path)
    rows = len(s.tab.rows())
    s.docs.fail("sheet.set_range_value")  # the first try fails after its insert: an empty third row
    processed(s.keeper, title="第一场", stream_id="v1")
    s.settle()
    processed(s.keeper, title="第二场", stream_id="v2")
    s.settle()
    assert titles(s.tab) == [] and s.docs.tools().count("sheet.insert_dimension") == 1
    assert [j.row.title for j in waiting(tmp_path)] == ["第一场", "第二场"]

    clock.t += INTERVAL
    s.settle()
    assert titles(s.tab) == ["第二场", "第一场"]
    assert len(s.tab.rows()) == rows + 2  # no empty row left behind
    assert s.tab.merges_of(2) == s.tab.merges_of(3) == list(SPANS)
    assert s.tab.texts(4)[0] == "主播1"
    assert waiting(tmp_path) == [] and alerts == []


def test_a_row_of_another_tab_does_not_wait(tmp_path):
    docs = FakeTencentDocs()
    record_sheet(docs)
    other = record_sheet(docs, OTHER.file_id, OTHER.tab_id)
    store(tmp_path, ("G1", {"streamers": ["A"], "source_record_sheet": SHEET}),
          ("G2", {"streamers": ["B"], "source_record_sheet": OTHER}))
    s, _, _ = setup(tmp_path, docs)
    docs.fail("sheet.get_cell_data")
    processed(s.keeper, title="第一场", stream_id="v1")
    s.settle()
    processed(s.keeper, B, title="别的组的", stream_id="v9")
    s.settle()
    assert titles(other) == ["别的组的"] and titles(s.tab) == []
    assert [j.row.title for j in waiting(tmp_path)] == ["第一场"]


def test_a_row_given_up_on_lets_the_next_of_its_tab_go(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.fail("sheet.get_cell_data", times=3)
    processed(s.keeper, title="第一场", stream_id="v1")
    processed(s.keeper, title="第二场", stream_id="v2")
    for _ in range(3):
        s.settle()
        clock.t += INTERVAL
    assert len(alerts) == 1 and "第一场" in alerts[0]
    assert titles(s.tab) == ["第二场"] and waiting(tmp_path) == []


def test_the_order_survives_a_restart(tmp_path):
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.set_range_value")
    processed(s.keeper, title="第一场", stream_id="v1")
    s.settle()
    processed(s.keeper, title="第二场", stream_id="v2")

    again = setup(tmp_path, s.docs, clock)[0]  # the service started again on the same files
    again.settle()
    assert titles(again.tab) == []
    clock.t += INTERVAL
    again.settle()
    assert titles(again.tab) == ["第二场", "第一场"]
    assert again.tab.texts(4)[0] == "主播1"
