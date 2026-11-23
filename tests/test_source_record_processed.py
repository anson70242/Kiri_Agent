"""A processed live is recorded in its groups' sheets: told that a live is fully processed
("全部处理完成") with its short download link, the module asks the QQ groups store which
groups record the streamer and writes one 录播 row into each of their tabs. Against a fake
Tencent Docs endpoint and a real groups store; made-up group ids, file ids, tab ids and links."""

from __future__ import annotations

import logging

from backend.monitor import Streamer
from backend.notify.qq import QQGroups, SourceRecordSheet

from .source_record_fakes import FILE_ID, LINK, TAB_ID, FakeTencentDocs, make, record_sheet

A_YOUTUBE, A_TWITCH = Streamer("A", "youtube", "@a"), Streamer("A", "twitch", "a_tw")
B = Streamer("B", "twitch", "b")
SHEET = SourceRecordSheet(FILE_ID, TAB_ID)
OTHER = SourceRecordSheet("OTHERFILE2", "tab002")
# 2026-12-23 00:30 in Hong Kong is still 2026-12-22 in UTC.
EXPIRES = 1_797_957_000.0


def store(tmp_path, *groups: tuple[str, dict]) -> QQGroups:
    """A groups store with these groups (openid, settings)."""
    s = QQGroups(tmp_path / "qq_groups.json")
    for openid, settings in groups:
        s.register(openid)
        s.update(openid, **settings)
    return s


def processed(keeper, streamer=A_YOUTUBE, **kw) -> None:
    keeper.processed(streamer, **{"date": "20261122", "title": "今日はのんびり雑談するよ〜", "link": LINK,
                                  "expires": EXPIRES, "stream_id": "v1", **kw})


def test_a_processed_live_of_a_ticked_streamer_gets_its_row_in_the_groups_sheet(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"name": "向日葵字幕组", "streamers": ["A"], "source_record_sheet": SHEET})))
    processed(s.keeper)
    s.settle()
    assert s.tab.texts(2) == ["A", "2026/11/22", "录播", "今日はのんびり雑談するよ〜（有效至 2026/12/23）\n全部下载"]
    assert s.tab.rich(2, 6)[1] == {"text": "全部下载", "link_url": LINK}


def test_a_streamer_no_group_records_causes_no_tencent_docs_call(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"streamers": ["A"], "source_record_sheet": SHEET}),
                          ("G2", {"streamers": ["B"]})))  # ticks B, but has no sheet
    processed(s.keeper, B)
    s.settle()
    assert s.docs.calls == []


def test_without_a_groups_store_nothing_is_written(tmp_path):
    s = make()
    processed(s.keeper)
    s.settle()
    assert s.docs.calls == []


def test_the_streamer_is_counted_by_person_on_every_platform(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"streamers": ["A"], "source_record_sheet": SHEET})))
    processed(s.keeper, A_TWITCH, title="Twitch 的那场")
    s.settle()
    assert s.tab.text(2, 0) == "A" and s.tab.text(2, 6).startswith("Twitch 的那场")


def test_the_groups_switches_and_notification_types_do_not_count_but_a_removed_group_gets_nothing(tmp_path):
    docs = FakeTencentDocs()
    mine, gone = record_sheet(docs), record_sheet(docs, OTHER.file_id, OTHER.tab_id)
    s = make(docs, groups=store(
        tmp_path, ("G1", {"enabled": False, "kinds": [], "streamers": ["A"], "source_record_sheet": SHEET}),
        ("G2", {"enabled": True, "streamers": ["A"], "source_record_sheet": OTHER, "status": "removed"})))
    processed(s.keeper)
    s.settle()
    assert mine.text(2, 0) == "A"
    assert gone.text(2, 0) == "主播1"


def test_every_tab_that_records_the_streamer_gets_a_row(tmp_path):
    docs = FakeTencentDocs()
    first, second = record_sheet(docs), record_sheet(docs, OTHER.file_id, OTHER.tab_id)
    s = make(docs, groups=store(tmp_path, ("G1", {"streamers": ["A"], "source_record_sheet": SHEET}),
                                ("G2", {"streamers": ["A", "B"], "source_record_sheet": OTHER})))
    processed(s.keeper)
    s.settle()
    assert first.text(2, 0) == second.text(2, 0) == "A"
    assert first.text(3, 0) == second.text(3, 0) == "主播1"


def test_two_groups_naming_one_tab_get_one_row(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"streamers": ["A"], "source_record_sheet": SHEET}),
                          ("G2", {"streamers": ["A"], "source_record_sheet": SHEET})))
    processed(s.keeper)
    s.settle()
    assert s.tab.text(2, 0) == "A" and s.tab.text(3, 0) == "主播1"


def test_the_log_names_the_group_the_streamer_and_the_live_and_never_the_link(tmp_path, caplog):
    s = make(groups=store(tmp_path, ("G1", {"name": "向日葵字幕组", "streamers": ["A"], "source_record_sheet": SHEET})))
    processed(s.keeper)
    with caplog.at_level(logging.DEBUG):
        s.settle()
    (written,) = [r.getMessage() for r in caplog.records if "row written" in r.getMessage()]
    assert "向日葵字幕组" in written and "A" in written and "今日はのんびり雑談するよ〜" in written
    assert LINK not in caplog.text
