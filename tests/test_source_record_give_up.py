"""The owner's message when a row is given up on: one operator alert, in Chinese, naming the
group, the streamer, the live, the kind of row and the reason, without a link; a tab without
one of the four headers is given up on at once; a sheet the token's account may not edit
is said to need sharing. No QQ group is told. Against a fake Tencent Docs endpoint and a fake
clock; made-up ids and links."""

from __future__ import annotations

import logging

from backend.notify.qq import SourceRecordSheet

from .source_record_fakes import FILE_ID, LINK, TAB_ID, FakeTencentDocs, record_sheet
from .test_source_record_processed import A_YOUTUBE, processed, store
from .test_source_record_retries import INTERVAL, ours, setup, waiting

SHEET = SourceRecordSheet(FILE_ID, TAB_ID)


def run_out(s, clock, tries: int = 3) -> None:
    for _ in range(tries):
        s.settle()
        clock.t += INTERVAL


def test_a_tab_without_a_header_is_given_up_on_at_once_and_the_message_names_it(tmp_path):
    docs = FakeTencentDocs()
    tab = record_sheet(docs, headers=("配信者", "日期", "项目类型", "链接"))
    before = tab.rows()
    store(tmp_path, ("G1", {"name": "章鱼烧字幕组", "streamers": ["A"], "source_record_sheet": SHEET}))
    s, clock, alerts = setup(tmp_path, docs)
    processed(s.keeper)
    s.settle()
    assert waiting(tmp_path) == [] and tab.rows() == before
    assert docs.tools() == ["sheet.get_cell_data"]  # one try, nothing changed
    (message,) = alerts
    assert "表头「闪传链接」" in message and "章鱼烧字幕组" in message


def test_a_sheet_the_tokens_account_may_not_edit_is_said_to_need_sharing(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.fail("sheet.insert_dimension", "code:400403, msg:no permission to edit this file", times=10)
    processed(s.keeper)
    run_out(s, clock)
    (message,) = alerts
    assert "no permission to edit this file" in message
    assert "共享给这个账号" in message and "编辑权限" in message


def test_another_failure_says_nothing_about_sharing(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.fail("sheet.insert_dimension", times=10)
    processed(s.keeper)
    run_out(s, clock)
    (message,) = alerts
    assert "共享" not in message


def test_the_message_names_the_kind_of_row_and_both_groups_naming_one_tab(tmp_path):
    store(tmp_path, ("G1", {"name": "章鱼烧字幕组", "streamers": ["A"], "source_record_sheet": SHEET}),
          ("G2", {"name": "鲷鱼烧字幕组", "streamers": ["A"], "source_record_sheet": SHEET}))
    docs = FakeTencentDocs()
    record_sheet(docs)
    s, clock, alerts = setup(tmp_path, docs)
    docs.fail("sheet.get_cell_data", times=10)
    s.keeper.uploaded(A_YOUTUBE, date="20261122", title="戦場がマイホームの声優です", link=LINK, stream_id="v1")
    run_out(s, clock)
    (message,) = alerts
    assert message.startswith("QQ 群「章鱼烧字幕组、鲷鱼烧字幕组」")
    assert "项目类型：网盘" in message and "直播：戦場がマイホームの声優です" in message


def test_neither_the_message_nor_the_log_holds_a_link(tmp_path, caplog):
    # A refused token: Tencent Docs' reason names the page to get a new one at.
    s, clock, alerts = setup(tmp_path)
    s.docs.expired = True
    processed(s.keeper)
    with caplog.at_level(logging.DEBUG):
        run_out(s, clock)
    (message,) = alerts
    assert "token" in message
    assert "http" not in message and "AbCdEf1234" not in message
    assert "AbCdEf1234" not in caplog.text


def test_an_unexpected_error_in_a_try_is_a_failed_try(tmp_path):
    s, clock, alerts = setup(tmp_path)
    calls = s.keeper.client

    class Broken:
        def post(self, *a, **kw):
            raise RuntimeError("boom")

        def close(self):
            pass

    s.keeper.client = Broken()
    processed(s.keeper)
    s.settle()
    assert waiting(tmp_path)[0].errors == 1
    s.keeper.client = calls
    clock.t += INTERVAL
    s.settle()
    assert len(ours(s)) == 1 and alerts == []

