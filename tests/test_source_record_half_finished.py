"""A try that failed half way never leads to two rows for one job: a retry finds the empty row
its earlier try inserted (the third row, still empty under the four headers) and goes on from
there; it finds a row the earlier try wrote although its answer was lost, and writes nothing
more; and when the empty row is no longer the third or no longer empty, it inserts a new row
and leaves the old one alone. Against a fake Tencent Docs endpoint and a fake clock; made-up
ids and links."""

from __future__ import annotations

from .test_source_record_processed import processed
from .test_source_record_retries import INTERVAL, ours, setup, waiting

ROW = ["A", "2026/11/22", "录播", "今日はのんびり雑談するよ〜（有效至 2026/12/23）\n全部下载"]
SPANS = [(0, 1), (2, 3), (4, 5), (6, 15)]


def retried(s, clock, tmp_path) -> None:
    """The first try, then the retry after the interval."""
    processed(s.keeper)
    s.settle()
    assert waiting(tmp_path)[0].errors == 1
    clock.t += INTERVAL
    s.settle()


def typed(s) -> list[list[str]]:
    """The rows people typed, below ours: they stay as they were."""
    return [r for r in s.tab.rows() if r and r[0].startswith("主播")]


def test_an_insert_whose_answer_was_lost_is_not_followed_by_a_second_row(tmp_path):
    s, clock, alerts = setup(tmp_path)
    rows = len(s.tab.rows())
    s.docs.fail("sheet.insert_dimension", applied=True)
    retried(s, clock, tmp_path)

    assert s.docs.tools().count("sheet.insert_dimension") == 1
    assert len(s.tab.rows()) == rows + 1
    assert s.tab.texts(2) == ROW and s.tab.merges_of(2) == SPANS
    assert waiting(tmp_path) == [] and alerts == []


def test_a_failed_merge_after_the_insert_goes_on_in_the_same_row(tmp_path):
    s, clock, _ = setup(tmp_path)
    rows = len(s.tab.rows())
    s.docs.fail("sheet.merge_cell")
    retried(s, clock, tmp_path)

    assert s.docs.tools().count("sheet.insert_dimension") == 1
    assert len(s.tab.rows()) == rows + 1
    assert s.tab.texts(2) == ROW and s.tab.merges_of(2) == SPANS
    assert s.tab.texts(3)[0] == "主播1" and s.tab.merges_of(3) == SPANS


def test_a_merge_done_before_the_failure_is_not_done_again(tmp_path):
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.merge_cell", applied=True)  # the first span is merged, its answer lost
    retried(s, clock, tmp_path)

    assert s.tab.texts(2) == ROW and s.tab.merges_of(2) == SPANS
    assert waiting(tmp_path) == []


def test_the_retry_after_a_restart_still_finds_the_empty_row(tmp_path):
    s, clock, _ = setup(tmp_path)
    rows = len(s.tab.rows())
    s.docs.fail("sheet.set_range_value")
    processed(s.keeper)
    s.settle()

    again, _, _ = setup(tmp_path, s.docs, clock)  # the service started again on the same files
    clock.t += INTERVAL
    again.settle()
    assert len(again.tab.rows()) == rows + 1 and again.tab.texts(2) == ROW


def test_cells_written_although_the_answer_was_lost_are_not_written_into_a_second_row(tmp_path):
    s, clock, alerts = setup(tmp_path)
    rows = len(s.tab.rows())
    s.docs.fail("sheet.set_range_value", applied=True)
    retried(s, clock, tmp_path)

    assert len(ours(s)) == 1 and len(s.tab.rows()) == rows + 1
    assert waiting(tmp_path) == [] and alerts == []
    processed(s.keeper)  # told again: the record knows the row is written
    s.settle()
    assert len(ours(s)) == 1


def test_a_row_written_although_the_answer_was_lost_is_found_below_a_row_typed_above_it(tmp_path):
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.set_range_value", applied=True)
    processed(s.keeper)
    s.settle()
    s.tab.insert_dimension({"dimension_type": "row", "index": 2, "direction": "before"})
    s.tab.fill_row(2, ["主播9", "2026/11/23", "杂谈", "有人先写的"])
    clock.t += INTERVAL
    s.settle()
    assert len(ours(s)) == 1 and waiting(tmp_path) == []


def test_an_empty_row_typed_into_is_left_alone_and_a_new_row_inserted(tmp_path):
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.merge_cell")
    processed(s.keeper)
    s.settle()
    s.tab.put(2, 0, "有人写了")
    clock.t += INTERVAL
    s.settle()

    # Merged like the row below it, which is that row: not merged, as the first try left it.
    assert s.tab.texts(2) == ROW and s.tab.merges_of(2) == s.tab.merges_of(3) == []
    assert s.tab.texts(3) == ["有人写了"]
    assert len(ours(s)) == 1


def test_an_empty_row_no_longer_third_is_left_alone_and_a_new_row_inserted(tmp_path):
    s, clock, _ = setup(tmp_path)
    s.docs.fail("sheet.merge_cell")
    processed(s.keeper)
    s.settle()
    s.tab.insert_dimension({"dimension_type": "row", "index": 2, "direction": "before"})
    s.tab.fill_row(2, ["主播9", "2026/11/23", "杂谈", "有人先写的"])
    clock.t += INTERVAL
    s.settle()

    assert s.tab.texts(2) == ROW
    assert s.tab.texts(3) == ["主播9", "2026/11/23", "杂谈", "有人先写的"]
    assert s.tab.texts(4) == []  # the empty row of the first try, left for a person
    assert len(ours(s)) == 1 and typed(s)[0][0] == "主播9"


def test_an_insert_that_failed_without_effect_is_followed_by_one_row(tmp_path):
    s, clock, _ = setup(tmp_path)
    rows = len(s.tab.rows())
    s.docs.fail("sheet.insert_dimension")
    retried(s, clock, tmp_path)
    assert len(s.tab.rows()) == rows + 1 and s.tab.texts(2) == ROW
    assert s.tab.texts(3)[0] == "主播1"
