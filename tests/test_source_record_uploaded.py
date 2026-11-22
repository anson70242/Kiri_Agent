"""An uploaded drive copy is recorded in its groups' sheets: told that a live's drive copy is
uploaded ("网盘上传完成") with its drive link, the module writes one 网盘 row into the tab of
every group that records the streamer, as it writes the 录播 row. Against a fake Tencent Docs
endpoint and a real groups store; made-up group ids, file ids, tab ids and links."""

from __future__ import annotations

import logging

from backend.notify.qq import SourceRecordSheet

from .source_record_fakes import FILE_ID, TAB_ID, FakeTencentDocs, make, record_sheet
from .test_source_record_processed import A_YOUTUBE, processed, store

SHEET = SourceRecordSheet(FILE_ID, TAB_ID)
DRIVE_LINK = "https://drive.example/s/MadeUp42?pwd=q7w8"  # the extraction code is inside the link


def uploaded(keeper, streamer=A_YOUTUBE, **kw) -> None:
    keeper.uploaded(streamer, **{"date": "20261122", "title": "戦場がマイホームの声優です", "link": DRIVE_LINK,
                                 "stream_id": "v1", **kw})


def test_an_uploaded_drive_copy_gets_a_drive_row_with_the_title_and_the_label_linking_to_the_drive_link(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"name": "章鱼烧字幕组", "streamers": ["A"], "source_record_sheet": SHEET})))
    uploaded(s.keeper)
    s.settle()
    assert s.tab.texts(2) == ["A", "2026/11/22", "网盘", "戦場がマイホームの声優です\n网盘链接"]
    assert s.tab.rich(2, 6) == [{"text": "戦場がマイホームの声優です\n"}, {"text": "网盘链接", "link_url": DRIVE_LINK}]
    assert s.tab.merges_of(2) == s.tab.merges_of(3) == [(0, 1), (2, 3), (4, 5), (6, 15)]
    assert s.tab.text(3, 0) == "主播1"


def test_a_group_that_does_not_tick_drive_copy_uploaded_still_gets_the_row(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"enabled": True, "kinds": ["ready"], "streamers": ["A"],
                                            "source_record_sheet": SHEET})))
    uploaded(s.keeper)
    s.settle()
    assert s.tab.texts(2)[:3] == ["A", "2026/11/22", "网盘"]


def test_after_the_archive_row_the_drive_row_goes_on_top(tmp_path):
    s = make(groups=store(tmp_path, ("G1", {"streamers": ["A"], "source_record_sheet": SHEET})))
    processed(s.keeper)
    s.settle()
    uploaded(s.keeper)
    s.settle()
    assert [s.tab.text(r, 4) for r in range(2, 5)] == ["网盘", "录播", "杂谈"]
    assert s.tab.text(3, 6) == "戦場がマイホームの声優です（有效至 2026/12/23）\n全部下载"


def test_the_drive_row_goes_under_the_sheets_own_headers_wherever_they_are(tmp_path):
    docs = FakeTencentDocs()
    # 闪传链接 first, then an extra column, 项目类型, 日期 and 配信者: each one column wide.
    tab = record_sheet(docs, headers=("闪传链接", "备注", "项目类型", "日期", "配信者"),
                       spans=((0, 0), (1, 1), (2, 2), (3, 3), (4, 4)))
    s = make(docs, groups=store(tmp_path, ("G1", {"streamers": ["A"], "source_record_sheet": SHEET})))
    uploaded(s.keeper)
    s.settle()
    assert [tab.text(2, c) for c in range(5)] == ["戦場がマイホームの声優です\n网盘链接", "", "网盘", "2026/11/22", "A"]


def test_the_drive_link_is_never_written_to_the_log(tmp_path, caplog):
    s = make(groups=store(tmp_path, ("G1", {"name": "章鱼烧字幕组", "streamers": ["A"], "source_record_sheet": SHEET})))
    with caplog.at_level(logging.DEBUG):
        uploaded(s.keeper)
        s.settle()
    (written,) = [r.getMessage() for r in caplog.records if "row written" in r.getMessage()]
    assert "网盘" in written and "章鱼烧字幕组" in written and "戦場がマイホームの声優です" in written
    assert DRIVE_LINK not in caplog.text
