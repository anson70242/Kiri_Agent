"""A processed live told with its flash link: the caller says which link it tells, the short
download link (as today) or the flash link. A flash link's 录播 row has the title with the
flash link's expiry on its first line and the link behind 「闪传链接」. Each live still gets
one 录播 row a tab, whichever link it was told with first. Against a fake Tencent Docs
endpoint and a real groups store; made-up ids and links."""

from __future__ import annotations

import json

from backend.notify.qq import QQGroups, SourceRecordSheet

from .source_record_fakes import FILE_ID, LINK, TAB_ID, make
from .test_source_record_processed import A_YOUTUBE, processed, store

SHEET = SourceRecordSheet(FILE_ID, TAB_ID)
RECORDING = ("G1", {"name": "向日葵字幕组", "streamers": ["A"], "source_record_sheet": SHEET})
FLASH_LINK = "https://flash.example.test/f/FlAsH5678"
# 2026-12-07 02:00 in Hong Kong is still 2026-12-06 in UTC.
FLASH_EXPIRES = 1_796_580_000.0
DRIVE_LINK = "https://example.test/s/drive1"


def flashed(keeper, **kw) -> None:
    processed(keeper, **{"link": FLASH_LINK, "expires": FLASH_EXPIRES, "flash": True, **kw})


def ours(tab) -> list[str]:
    """The link cells of the rows the module wrote (the typed rows start with 旧的直播)."""
    return [r[3] for r in tab.rows()[2:] if not r[3].startswith("旧的直播")]


def test_a_live_told_with_its_flash_link_gets_a_row_with_the_link_behind_its_label(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    flashed(s.keeper)
    s.settle()
    assert s.tab.texts(2) == ["A", "2026/11/22", "录播", "今日はのんびり雑談するよ〜（有效至 2026/12/7）\n闪传链接"]
    assert s.tab.rich(2, 6)[1] == {"text": "闪传链接", "link_url": FLASH_LINK}
    assert s.tab.merges_of(2) == s.tab.merges_of(3)


def test_a_live_told_with_its_short_download_link_gets_the_row_of_today(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper, flash=False)
    s.settle()
    assert s.tab.texts(2) == ["A", "2026/11/22", "录播", "今日はのんびり雑談するよ〜（有效至 2026/12/23）\n全部下载"]
    assert s.tab.rich(2, 6)[1] == {"text": "全部下载", "link_url": LINK}


def test_the_drive_row_of_a_live_told_with_its_flash_link_is_as_before(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    flashed(s.keeper)
    s.keeper.uploaded(A_YOUTUBE, date="20261122", title="今日はのんびり雑談するよ〜", link=DRIVE_LINK, stream_id="v1")
    s.settle()
    assert s.tab.texts(2) == ["A", "2026/11/22", "网盘", "今日はのんびり雑談するよ〜\n网盘链接"]
    assert s.tab.rich(2, 6)[1] == {"text": "网盘链接", "link_url": DRIVE_LINK}
    assert s.tab.text(3, 4) == "录播"


def test_a_live_told_with_its_flash_link_is_not_told_again_with_its_short_download_link(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    flashed(s.keeper)
    s.settle()
    processed(s.keeper)
    s.settle()
    assert ours(s.tab) == ["今日はのんびり雑談するよ〜（有效至 2026/12/7）\n闪传链接"]


def test_a_live_told_with_both_links_before_its_row_is_written_gets_the_first_one_only(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper)
    flashed(s.keeper)
    s.settle()
    assert [c.split("\n")[1] for c in ours(s.tab)] == ["全部下载"]


def test_a_live_told_with_one_link_is_not_told_with_the_other_after_a_restart(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    processed(s.keeper)
    s.settle()

    again = make(s.docs, groups=QQGroups(tmp_path / "qq_groups.json"))
    flashed(again.keeper)
    again.settle()
    assert [c.split("\n")[1] for c in ours(again.tab)] == ["全部下载"]


def test_a_flash_link_waiting_when_the_service_stops_is_written_behind_its_label_after_the_restart(tmp_path):
    s = make(groups=store(tmp_path, RECORDING))
    flashed(s.keeper)  # told, not written yet

    again = make(s.docs, groups=QQGroups(tmp_path / "qq_groups.json"))
    again.settle()
    assert again.tab.rich(2, 6)[1] == {"text": "闪传链接", "link_url": FLASH_LINK}


def test_a_job_saved_before_flash_links_existed_still_loads_and_holds_a_short_download_link(tmp_path):
    groups = store(tmp_path, RECORDING)
    (tmp_path / "source_record_jobs.json").write_text(json.dumps([{
        "tab": {"file_id": FILE_ID, "tab_id": TAB_ID},
        "row": {"kind": "录播", "streamer": "A", "date": "20261122", "title": "今日はのんびり雑談するよ〜",
                "link": LINK, "expires": 1_797_957_000.0, "stream_id": "v1"},
        "group": "向日葵字幕组", "id": "0123456789ab", "errors": 0, "next_try": 0.0, "inserted": False,
    }], ensure_ascii=False), encoding="utf-8")
    s = make(groups=groups)
    s.settle()
    assert s.tab.texts(2) == ["A", "2026/11/22", "录播", "今日はのんびり雑談するよ〜（有效至 2026/12/23）\n全部下载"]
    assert s.tab.rich(2, 6)[1] == {"text": "全部下载", "link_url": LINK}
