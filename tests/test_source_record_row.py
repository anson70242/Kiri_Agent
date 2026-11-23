"""A row in a source record sheet: told an archive's short download link for a tab, the
worker puts the 录播 row into that tab, as its third row, against a fake Tencent Docs endpoint."""

from __future__ import annotations

import asyncio
import logging

from backend.source_record_sheet import RowKind, SheetRow, SheetTab

from .source_record_fakes import LINK, FakeTencentDocs, make, record_sheet

# 2026-12-23 00:30 in Hong Kong is still 2026-12-22 in UTC.
EXPIRES = 1_797_957_000.0


def archive_row(**kw) -> SheetRow:
    return SheetRow(**{"kind": RowKind.ARCHIVE, "streamer": "森野鈴", "date": "20261105",
                       "title": "今日はのんびり雑談するよ〜", "link": LINK, "expires": EXPIRES, **kw})


def test_the_row_goes_in_as_the_third_row_and_the_rows_there_move_down():
    s = make()
    before = s.tab.rows()
    merges_before = [s.tab.merges_of(r) for r in range(len(before))]
    s.keeper.add(s.sheet, archive_row())
    s.settle()
    rows = s.tab.rows()
    assert len(rows) == len(before) + 1
    assert rows[:2] == before[:2]
    assert rows[2][0] == "森野鈴"
    assert rows[3:] == before[2:]
    assert [s.tab.merges_of(r) for r in (0, 1, *range(3, len(rows)))] == merges_before


def test_the_new_row_is_merged_like_the_row_below_it():
    s = make()
    s.keeper.add(s.sheet, archive_row())
    s.settle()
    assert s.tab.merges_of(2) == [(0, 1), (2, 3), (4, 5), (6, 15)]


def test_the_spans_are_the_sheets_own_not_a_fixed_layout():
    docs = FakeTencentDocs()
    record_sheet(docs, spans=((0, 2), (3, 3), (4, 4), (5, 9)))
    s = make(docs)
    s.keeper.add(s.sheet, archive_row())
    s.settle()
    assert s.tab.merges_of(2) == [(0, 2), (5, 9)]
    assert s.tab.merges_of(3) == [(0, 2), (5, 9)]


def test_a_sheet_whose_row_below_has_no_merges_gets_a_row_without_merges():
    docs = FakeTencentDocs()
    record_sheet(docs, spans=((0, 0), (1, 1), (2, 2), (3, 3)))
    s = make(docs)
    s.keeper.add(s.sheet, archive_row())
    s.settle()
    assert s.tab.merges_of(2) == []
    assert s.tab.texts(2)[:3] == ["森野鈴", "2026/11/5", "录播"]
    assert "merge_cell" not in " ".join(docs.tools())


def test_the_values_go_under_the_headers_wherever_they_are():
    docs = FakeTencentDocs()
    tab = docs.tab()
    for col, header in {0: "日期", 1: "备注", 3: "配信者", 4: "闪传链接", 7: "项目类型", 8: "路灯链接"}.items():
        tab.put(0, col, header)
    tab.put(1, 0, "路灯表")
    tab.put(2, 3, "主播1")
    s = make(docs)
    s.keeper.add(s.sheet, archive_row(date="20261122"))
    s.settle()
    assert {c: tab.text(2, c) for r, c in tab.cells if r == 2} == {
        0: "2026/11/22", 3: "森野鈴", 4: "今日はのんびり雑談するよ〜（有效至 2026/12/23）\n全部下载", 7: "录播"}
    assert tab.texts(3) == ["主播1"]


def test_the_row_names_the_streamer_the_kind_and_the_archives_date_without_leading_zeros():
    s = make()
    s.keeper.add(s.sheet, archive_row(date="20260105"))
    s.settle()
    assert [s.tab.text(2, c) for c in (0, 2, 4)] == ["森野鈴", "2026/1/5", "录播"]


def test_the_link_cell_holds_the_title_the_expiry_and_the_label_linking_to_the_address():
    s = make()
    s.keeper.add(s.sheet, archive_row())
    s.settle()
    assert s.tab.rich(2, 6) == [{"text": "今日はのんびり雑談するよ〜（有效至 2026/12/23）\n"},
                                {"text": "全部下载", "link_url": LINK}]
    assert LINK not in s.tab.text(2, 6)


def test_the_expiry_is_a_hong_kong_date_when_utc_is_on_another_day():
    # 2026-12-31 23:30 UTC is 2027-01-01 07:30 in Hong Kong.
    s = make()
    s.keeper.add(s.sheet, archive_row(expires=1_798_759_800.0))
    s.settle()
    assert s.tab.text(2, 6).split("\n")[0] == "今日はのんびり雑談するよ〜（有效至 2027/1/1）"


def test_a_tab_without_a_header_gets_no_row_and_the_failure_names_the_header(caplog):
    docs = FakeTencentDocs()
    tab = record_sheet(docs, headers=("配信者", "日期", "项目类型", "链接"))
    before = tab.rows()
    s = make(docs)
    s.keeper.add(s.sheet, archive_row())
    with caplog.at_level(logging.INFO):
        s.settle()
    assert tab.rows() == before
    assert "sheet.insert_dimension" not in docs.tools()
    assert "闪传链接" in caplog.text
    assert "row not written" in caplog.text


def test_the_log_says_the_row_was_written_and_never_the_link(caplog):
    s = make()
    s.keeper.add(s.sheet, archive_row())
    with caplog.at_level(logging.DEBUG):
        s.settle()
    written = [r.getMessage() for r in caplog.records if "row written" in r.getMessage()]
    assert len(written) == 1 and "森野鈴" in written[0] and "今日はのんびり雑談するよ〜" in written[0]
    assert "录播" in written[0]
    assert LINK not in caplog.text and "AbCdEf1234" not in caplog.text


def test_a_failed_call_is_logged_without_the_link_and_the_next_row_of_the_tab_waits_for_it(caplog):
    now = [1_800_000_000.0]
    s = make(clock=lambda: now[0], retry_interval=900)
    s.docs.fail("sheet.get_cell_data", "code:400007, msg:internal error")
    s.keeper.add(s.sheet, archive_row(title="第一场", stream_id="v1"))
    s.keeper.add(s.sheet, archive_row(title="第二场", link=LINK + "X", stream_id="v2"))
    with caplog.at_level(logging.DEBUG):
        s.settle()
    assert "row not written" in caplog.text and "400007" in caplog.text
    assert "AbCdEf1234" not in caplog.text
    assert s.tab.text(2, 0) == "主播1"  # nothing written yet: a tab's rows go in the order told
    assert not asyncio.run(s.keeper.write_next())  # the failed row waits for its retry, the next behind it
    now[0] += 900
    s.settle()  # both are written then, the later on top
    assert [s.tab.text(r, 6).split("（")[0] for r in (2, 3)] == ["第二场", "第一场"]


def test_rows_told_one_after_another_are_both_written_the_later_on_top():
    s = make()
    s.keeper.add(s.sheet, archive_row(title="第一场", stream_id="v1"))
    s.keeper.add(s.sheet, archive_row(title="第二场", stream_id="v2"))
    s.settle()
    assert [s.tab.text(r, 6).split("（")[0] for r in (2, 3)] == ["第二场", "第一场"]
    assert s.tab.merges_of(2) == s.tab.merges_of(3) == s.tab.merges_of(4)


def test_each_row_goes_into_its_own_tab():
    docs = FakeTencentDocs()
    first, second = record_sheet(docs), record_sheet(docs, "OTHERFILE2", "tab002")
    s = make(docs)
    s.keeper.add(SheetTab("OTHERFILE2", "tab002"), archive_row(title="别的表"))
    s.settle()
    assert second.text(2, 6).startswith("别的表")
    assert first.text(2, 0) == "主播1"
