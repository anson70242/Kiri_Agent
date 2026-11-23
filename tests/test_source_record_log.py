"""The log of the source record sheet: never a link, not even one Tencent Docs' reason echoes,
and in English (a missing header is named, the owner's alert stays Chinese). Against a fake
Tencent Docs endpoint and a fake clock; made-up ids and links."""

from __future__ import annotations

import logging

from backend.notify.qq import SourceRecordSheet
from backend.source_record_sheet import RowJob, RowKind, SheetRow, SheetTab

from .source_record_fakes import FILE_ID, LINK, TAB_ID, FakeTencentDocs, record_sheet
from .test_source_record_processed import processed, store
from .test_source_record_retries import setup

SHEET = SourceRecordSheet(FILE_ID, TAB_ID)


def test_a_reason_that_echoes_the_link_puts_no_link_into_the_log(tmp_path, caplog):
    s, _, _ = setup(tmp_path)
    s.docs.fail("sheet.set_range_value", f"code:400010, msg:invalid link_url {LINK} in rich_string")
    processed(s.keeper)
    with caplog.at_level(logging.DEBUG):
        s.settle()
    assert "invalid link_url" in caplog.text  # the reason is logged
    assert "AbCdEf1234" not in caplog.text and "example.test" not in caplog.text


def test_a_missing_header_is_logged_in_english_naming_the_header(tmp_path, caplog):
    docs = FakeTencentDocs()
    record_sheet(docs, headers=("配信者", "日期", "项目类型", "链接"))
    store(tmp_path, ("G1", {"name": "向日葵字幕组", "streamers": ["A"], "source_record_sheet": SHEET}))
    s, _, alerts = setup(tmp_path, docs)
    processed(s.keeper)
    with caplog.at_level(logging.DEBUG):
        s.settle()
    (line,) = [r.getMessage() for r in caplog.records if "not written" in r.getMessage()]
    assert "header 闪传链接" in line
    assert "表头" not in line and "表格" not in line
    (message,) = alerts
    assert "表格第一行没有表头「闪传链接」" in message  # the owner's alert stays Chinese


def test_a_row_and_its_job_shown_with_repr_hold_no_link():
    row = SheetRow(RowKind.ARCHIVE, "A", "20261122", "今日はのんびり雑談するよ〜", LINK, None, "v1")
    job = RowJob(SheetTab(FILE_ID, TAB_ID), row, "向日葵字幕组")
    assert "AbCdEf1234" not in repr(row) and "AbCdEf1234" not in repr(job)
    assert "今日はのんびり雑談するよ〜" in repr(job)
