"""Writing one row into one tab, newest on top:

1. read the first row and find the columns of the four headers by their text;
2. read the merges of the row that is third now;
3. insert one row before it (it has no merges of its own);
4. merge the new row in the same column spans;
5. write the four cells.

Four calls, plus one per merged span (eight for a row merged in four spans). A missing
header stops it before anything is changed.

A try that may have inserted its row (it got to step 3, whatever the answer) leaves the
next try to look before it inserts again, so that one job never fills two rows:

- the row is already there, written by that try although its answer was lost: one of the
  rows from the third down holds this row's three text cells, and its link cell's first line
  is this row's (the live's title, and a short download link's expiry). The label line
  behind the link is not compared: how the endpoint reads rich text back was never seen.
  Nothing more is written.
- the third row is still empty under the four headers: the row that try inserted. It is
  merged like the row below it (the spans not merged yet) and written, from step 4.
- else (somebody typed into it, or inserted a row above it; or the insert never happened)
  a new row is inserted, and an empty row left behind stays for a person.

The look costs one call (two more when the empty row is taken).
"""

from __future__ import annotations

from collections.abc import Callable

from backend.tencent_docs import CellValue, SheetTabCalls

from .row import HEADERS, SheetRow

HEADER_ROW = 0
NEW_ROW = 2  # the third row: the first two (the headers, the link to the highlight workbook) stay on top
# The last column read for the headers and the merges (AB): checked against the live endpoint
# on 2026-11-22. A header further right is not found.
LAST_COL = 27
# Rows from the third down where a retry looks for its row written by an earlier try:
# rows people insert above it in the retry's wait push it down.
LOOK_DOWN = 20


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


def write_row(tab: SheetTabCalls, row: SheetRow, *, again: bool = False,
              inserting: Callable[[], None] = lambda: None) -> None:
    """Put `row` into the tab as its third row, merged like the row below it. `again`: an
    earlier try may have inserted it (see above). `inserting` is called before the insert
    is sent (the job notes it). Raises MissingHeader or TencentDocsError."""
    columns = header_columns(tab)
    cells = {columns[h]: value for h, value in row.cells().items()}
    if again:
        below = tab.texts(NEW_ROW, NEW_ROW + LOOK_DOWN - 1, LAST_COL)
        expected = {col: value if isinstance(value, str) else first_line("".join(s.text for s in value))
                    for col, value in cells.items()}

        def ours(r: int) -> bool:
            """Row `r` holds the three text cells, and the link cell's first line."""
            for col, text in expected.items():
                got = below.get((r, col), "")
                if (got if isinstance(cells[col], str) else first_line(got)) != text:
                    return False
            return True

        if any(ours(r) for r in range(NEW_ROW, NEW_ROW + LOOK_DOWN)):
            return  # written by the earlier try
        if not any(below.get((NEW_ROW, col)) for col in columns.values()):
            done = tab.row_merges(NEW_ROW, LAST_COL)
            spans = [s for s in tab.row_merges(NEW_ROW + 1, LAST_COL) if s not in done]
            fill(tab, spans, cells)
            return
    spans = tab.row_merges(NEW_ROW, LAST_COL)
    inserting()
    tab.insert_row(NEW_ROW)
    fill(tab, spans, cells)


def first_line(text: str) -> str:
    """A link cell's first line: the live's title (and a short download link's expiry)."""
    return text.split("\n", 1)[0].strip()


def fill(tab: SheetTabCalls, spans: list[tuple[int, int]], cells: dict[int, CellValue]) -> None:
    """Steps 4 and 5 on the third row."""
    for first, last in spans:
        tab.merge(NEW_ROW, first, last)
    tab.write(NEW_ROW, cells)
