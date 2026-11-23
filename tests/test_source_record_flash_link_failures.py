"""A flash link's row that cannot be written is tried again and given up on as a short
download link's is, with the same owner's message, and a flash link is in no log line and
no message. Against a fake Tencent Docs endpoint and a fake clock; made-up ids and links."""

from __future__ import annotations

import logging

from .test_source_record_flash_link import FLASH_LINK, flashed
from .test_source_record_retries import INTERVAL, setup, waiting

FLASH_ID = "FlAsH5678"


def flash_rows(s) -> list[str]:
    """The rows the module wrote with a flash link: their link cell's text."""
    return [s.tab.text(r, 6) for r in range(2, s.tab.row_count()) if s.tab.text(r, 6).endswith("\n闪传链接")]


def test_a_failed_try_of_a_flash_link_waits_for_the_retry_interval_and_survives_a_restart(tmp_path):
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.get_cell_data")
    flashed(s.keeper)
    s.settle()
    (job,) = waiting(tmp_path)
    assert (job.errors, job.next_try) == (1, clock.t + INTERVAL)

    again, _, alerts = setup(tmp_path, s.docs, clock)  # the service started again on the same files
    clock.t += INTERVAL - 1
    again.settle()
    assert flash_rows(again) == []

    clock.t += 1
    again.settle()
    assert len(flash_rows(again)) == 1 and waiting(tmp_path) == [] and alerts == []
    assert again.tab.rich(2, 6)[1] == {"text": "闪传链接", "link_url": FLASH_LINK}


def test_after_the_last_failed_try_of_a_flash_link_the_owner_gets_the_message_of_today(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.fail("sheet.get_cell_data", f"code:400010, msg:invalid link_url {FLASH_LINK}", times=10)
    flashed(s.keeper)
    for _ in range(5):
        s.settle()
        clock.t += INTERVAL
    assert len(s.docs.tools()) == 3  # three tries, one call each
    assert waiting(tmp_path) == [] and flash_rows(s) == []
    assert alerts == ["\n".join([
        "QQ 群「向日葵字幕组」的录播源记录表没写进这一行，已放弃，请手动补上",
        "配信者：A",
        "日期：2026/11/22",
        "直播：今日はのんびり雑談するよ〜",
        "项目类型：录播",
        "原因：Tencent Docs sheet.get_cell_data failed: code:400010, msg:invalid link_url （地址略）",
    ])]


def test_a_flash_link_row_whose_answer_was_lost_is_not_written_twice(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.fail("sheet.set_range_value", applied=True)  # written, but the answer is lost
    flashed(s.keeper)
    s.settle()
    clock.t += INTERVAL
    s.settle()
    assert len(flash_rows(s)) == 1 and waiting(tmp_path) == [] and alerts == []


def test_a_flash_link_is_in_no_log_line_written_or_failed(tmp_path, caplog):
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.set_range_value", f"code:400010, msg:invalid link_url {FLASH_LINK} in rich_string")
    flashed(s.keeper)
    with caplog.at_level(logging.DEBUG):
        s.settle()
        clock.t += INTERVAL
        s.settle()
    assert "invalid link_url" in caplog.text and "row written" in caplog.text
    assert FLASH_ID not in caplog.text and "flash.example.test" not in caplog.text
    assert len(flash_rows(s)) == 1


def test_a_flash_link_is_kept_only_while_its_job_waits(tmp_path):
    s, _, _ = setup(tmp_path)
    flashed(s.keeper)
    assert FLASH_LINK in s.keeper.jobs.path.read_text(encoding="utf-8")
    s.settle()
    for f in tmp_path.glob("source_record_*.json"):
        assert FLASH_ID not in f.read_text(encoding="utf-8"), f.name
