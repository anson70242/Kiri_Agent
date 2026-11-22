"""Writing one row into one tab, newest on top:

1. read the first row and find the columns of the four headers by their text;
2. read the merges of the row that is third now;
3. insert one row before it (it has no merges of its own);
4. merge the new row in the same column spans;
5. write the four cells.

Four calls, plus one per merged span (eight for a row merged in four spans). A missing
header stops it before anything is changed.
"""

from __future__ import annotations

from backend.tencent_docs import SheetTabCalls

from .row import HEADERS, SheetRow

HEADER_ROW = 0
NEW_ROW = 2  # the third row: the first two (the headers, the link to the highlight workbook) stay on top
# The last column read for the headers and the merges (AB): checked against the live endpoint
# on 2026-11-22. A header further right is not found.
LAST_COL = 27


class MissingHeader(Exception):
    """The tab's first row lacks one of the headers: trying again cannot help."""

    def __init__(self, header: str) -> None:
        super().__init__(f"表格第一行没有表头「{header}」")
        self.header = header


def header_columns(tab: SheetTabCalls) -> dict[str, int]:
    """The column of each header, by its text in the first row (the leftmost, if twice)."""
    found: dict[str, int] = {}
    for (_, col), text in sorted(tab.texts(HEADER_ROW, HEADER_ROW, LAST_COL).items()):
        found.setdefault(text.strip(), col)
    for header in HEADERS:
        if header not in found:
            raise MissingHeader(header)
    return {h: found[h] for h in HEADERS}


def write_row(tab: SheetTabCalls, row: SheetRow) -> None:
    """Put `row` into the tab as its third row, merged like the row below it. Raises
    MissingHeader or TencentDocsError."""
    columns = header_columns(tab)
    spans = tab.row_merges(NEW_ROW, LAST_COL)
    tab.insert_row(NEW_ROW)
    for first, last in spans:
        tab.merge(NEW_ROW, first, last)
    tab.write(NEW_ROW, {columns[h]: value for h, value in row.cells().items()})
