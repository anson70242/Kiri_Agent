"""A retry that looks for its row written by an earlier try leans on the link cell's rich text
read back as little as it can: how the live endpoint reads a RICH_STRING cell back was never
seen. Its row is found by the three plain cells and the link cell's first line (the live's
title, and a short download link's expiry), whatever the second line reads back as; two lives
of one streamer on one day are still told apart. Against a fake Tencent Docs endpoint and a
fake clock; made-up ids and links."""

from __future__ import annotations

from .source_record_fakes import LINK
from .test_source_record_processed import A_YOUTUBE, processed
from .test_source_record_retries import INTERVAL, setup, waiting


def link_cells(tab) -> list[str]:
    """The link cells the module wrote, top down (as stored, not as read back)."""
    return [tab.text(r, 6) for r in range(tab.row_count()) if tab.rich(r, 6)]


def uploaded(keeper, title: str, stream_id: str) -> None:
    keeper.uploaded(A_YOUTUBE, date="20261122", title=title, link=LINK, stream_id=stream_id)


def test_a_row_written_although_its_answer_was_lost_is_found_when_its_link_reads_back_otherwise(tmp_path):
    s, clock, alerts = setup(tmp_path)
    s.docs.links_read_back_as_addresses = True
    rows = len(s.tab.rows())
    s.docs.fail("sheet.set_range_value", applied=True)
    processed(s.keeper)
    s.settle()
    clock.t += INTERVAL
    s.settle()
    assert s.docs.tools().count("sheet.insert_dimension") == 1
    assert len(s.tab.rows()) == rows + 1 and len(link_cells(s.tab)) == 1
    assert waiting(tmp_path) == [] and alerts == []


def test_a_title_that_begins_another_lives_title_is_not_taken_for_it(tmp_path):
    s, clock, _ = setup(tmp_path)
    s.docs.links_read_back_as_addresses = True
    rows = len(s.tab.rows())
    uploaded(s.keeper, "雑談 #2", "v1")
    s.settle()
    s.docs.fail("sheet.set_range_value")  # the second live's try leaves an empty third row
    uploaded(s.keeper, "雑談", "v2")
    s.settle()
    clock.t += INTERVAL
    s.settle()
    assert link_cells(s.tab) == ["雑談\n网盘链接", "雑談 #2\n网盘链接"]
    assert len(s.tab.rows()) == rows + 2 and waiting(tmp_path) == []
