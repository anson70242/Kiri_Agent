"""Fakes for the source record sheet: Tencent Docs' MCP endpoint (JSON-RPC over HTTP, behind
httpx.MockTransport) holding sheet tabs in memory, and a builder that wires it into a
SourceRecordKeeper. Every source record sheet test imports from here.

The endpoint answers the calls the module makes, the way the live endpoint did on
2026-11-22: reading cells, reading the merges of a range (answered as `<tab id>$A3:B3`),
inserting a row (the new row has no merges), merging (the first cell's value is kept) and
writing cells (STRING and RICH_STRING). `fail` makes a call answer an error;
`forbidden` answers every call HTTP 403; `links_read_back_as_addresses` reads rich text back
other than it was written.

File ids, tab ids, names and links are invented."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from backend.notify.qq import QQGroups
from backend.source_record_sheet import SheetTab, SourceRecordKeeper
from backend.tencent_docs import TencentSettings

FILE_ID = "FAKESHEETFILE1"
TAB_ID = "tab001"
TOKEN = "tok-SHEET-SECRET"
HEADERS = ("配信者", "日期", "项目类型", "闪传链接")
# The column spans of a row in the default layout: 配信者 A:B, 日期 C:D, 项目类型 E:F, the link G:P.
SPANS = ((0, 1), (2, 3), (4, 5), (6, 15))
LINK = "https://example.test/d/AbCdEf1234"


def column_letters(col: int) -> str:
    letters = ""
    col += 1
    while col:
        col, rest = divmod(col - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


class FakeTab:
    """One tab: cells by (row, col), 0-based, and merges as (first row, first col, last row, last col)."""

    def __init__(self, tab_id: str) -> None:
        self.tab_id = tab_id
        self.cells: dict[tuple[int, int], dict[str, Any]] = {}
        self.merges: list[tuple[int, int, int, int]] = []

    # ---- setting a tab up ----

    def put(self, row: int, col: int, value: str | list[dict[str, str]]) -> None:
        """A cell's value: text, or rich text segments ({"text", "link_url"?})."""
        if isinstance(value, list):
            self.cells[row, col] = {"value_type": "RICH_STRING", "rich_string": [dict(s) for s in value]}
        else:
            self.cells[row, col] = {"value_type": "STRING", "string_value": value}

    def merge(self, row: int, first: int, last: int) -> None:
        self.merges.append((row, first, row, last))

    def fill_row(self, row: int, values: list[str], spans=SPANS) -> None:
        """A row as people typed it: merged in `spans`, one value at the start of each span."""
        for (first, last), value in zip(spans, values):
            if last > first:
                self.merge(row, first, last)
            if value:
                self.put(row, first, value)

    # ---- reading a tab in tests ----

    def text(self, row: int, col: int) -> str:
        """A cell's text as the sheet shows it (rich text segments joined)."""
        cell = self.cells.get((row, col)) or {}
        if cell.get("value_type") == "RICH_STRING":
            return "".join(s.get("text", "") for s in cell["rich_string"])
        return str(cell.get("string_value", ""))

    def rich(self, row: int, col: int) -> list[dict[str, str]]:
        return (self.cells.get((row, col)) or {}).get("rich_string") or []

    def texts(self, row: int) -> list[str]:
        """The row's non-empty cell texts, left to right."""
        return [self.text(r, c) for r, c in sorted(self.cells) if r == row and self.text(r, c)]

    def row_count(self) -> int:
        """Rows up to the last one holding a value or a merge."""
        rows = [r for r, _ in self.cells] + [m[2] for m in self.merges]
        return 1 + max(rows) if rows else 0

    def rows(self) -> list[list[str]]:
        return [self.texts(r) for r in range(self.row_count())]

    def merges_of(self, row: int) -> list[tuple[int, int]]:
        """The column spans merged within this one row."""
        return sorted((c1, c2) for r1, c1, r2, c2 in self.merges if r1 == r2 == row)

    # ---- the endpoint's calls ----

    def get_cell_data(self, a: dict[str, Any]) -> dict[str, Any]:
        cells = [{"row": r, "col": c, **cell, **({"string_value": self.text(r, c)} if "rich_string" in cell else {})}
                 for (r, c), cell in sorted(self.cells.items())
                 if a["start_row"] <= r <= a["end_row"] and a["start_col"] <= c <= a["end_col"]]
        return {"cells": cells}

    def get_merged_cells(self, a: dict[str, Any]) -> dict[str, Any]:
        found = self.merges
        if "start_row" in a:
            found = [m for m in self.merges if m[0] <= a["end_row"] and m[2] >= a["start_row"]
                     and m[1] <= a["end_col"] and m[3] >= a["start_col"]]
        else:
            found = found[:1]  # without a range the live endpoint answered a single merge
        return {"merged_cells": [f"{self.tab_id}${column_letters(c1)}{r1 + 1}:{column_letters(c2)}{r2 + 1}"
                                 for r1, c1, r2, c2 in found]}

    def insert_dimension(self, a: dict[str, Any]) -> dict[str, Any]:
        assert a["dimension_type"] == "row" and a["direction"] == "before", a
        at, n = a["index"], a.get("count", 1)
        self.cells = {(r + n if r >= at else r, c): v for (r, c), v in self.cells.items()}
        self.merges = [(r1 + n if r1 >= at else r1, c1, r2 + n if r2 >= at else r2, c2)
                       for r1, c1, r2, c2 in self.merges]
        return {}

    def merge_cell(self, a: dict[str, Any]) -> dict[str, Any]:
        assert a["merge_type"] == "all", a
        new = (a["start_row"], a["start_col"], a["end_row"], a["end_col"])
        for m in self.merges:
            if m[0] <= new[2] and m[2] >= new[0] and m[1] <= new[3] and m[3] >= new[1]:
                raise FakeRefusal("code:400001, msg:merge overlaps a merged range")
        self.merges.append(new)
        for r in range(new[0], new[2] + 1):
            for c in range(new[1], new[3] + 1):
                if (r, c) != (new[0], new[1]):
                    self.cells.pop((r, c), None)
        return {}

    def set_range_value(self, a: dict[str, Any]) -> dict[str, Any]:
        for v in a["values"]:
            kind = v["value_type"]
            if kind == "STRING":
                self.put(v["row"], v["col"], v["string_value"])
            elif kind == "RICH_STRING":
                self.put(v["row"], v["col"], v["rich_string"])
            else:
                raise FakeRefusal(f"code:400002, msg:value_type {kind} not handled by the fake")
        return {}


def read_back_as_address(cell: dict[str, Any]) -> dict[str, Any]:
    """A rich cell as the endpoint might answer it: no plain text, linked runs as their address."""
    if "rich_string" not in cell:
        return cell
    runs = [{**r, "text": r["link_url"]} if r.get("link_url") else r for r in cell["rich_string"]]
    return {**cell, "string_value": "", "rich_string": runs}


class FakeRefusal(Exception):
    """A call the fake answers with a JSON-RPC error carrying this message."""


@dataclass
class Failure:
    tool: str
    message: str
    times: int
    applied: bool  # the call takes effect, then answers the error (an answer lost on the way)


class FakeTencentDocs:
    """Tencent Docs' MCP endpoint with tabs in memory. `calls` notes every (tool, arguments)."""

    def __init__(self) -> None:
        self.tabs: dict[tuple[str, str], FakeTab] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: list[Failure] = []
        self.expired = False  # the token is refused with HTTP 401
        self.forbidden = False  # every call is refused with HTTP 403
        # How the live endpoint reads a RICH_STRING cell back was never seen: in this mode a
        # rich cell comes back without its plain text, and a linked run's text is its address.
        self.links_read_back_as_addresses = False

    def tab(self, file_id: str = FILE_ID, tab_id: str = TAB_ID) -> FakeTab:
        """The tab, made empty the first time it is asked for."""
        return self.tabs.setdefault((file_id, tab_id), FakeTab(tab_id))

    def fail(self, tool: str, message: str = "code:400007, msg:internal error", *, times: int = 1,
             applied: bool = False) -> None:
        """The next `times` calls of `tool` answer a JSON-RPC error with `message`; `applied`:
        the call still takes effect (its answer is lost)."""
        self.failures.append(Failure(tool, message, times, applied))

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.expired:
            return httpx.Response(401, json={"error": "invalid_token", "error_description": "invalid or expired token"})
        if self.forbidden:
            return httpx.Response(403, json={"error": "forbidden"})
        assert request.headers["Authorization"] == TOKEN
        body = json.loads(request.content)
        assert body["method"] == "tools/call"
        tool, args = body["params"]["name"], body["params"]["arguments"]
        self.calls.append((tool, args))

        def error(message: str) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32603, "message": message}})

        failure = next((f for f in self.failures if f.tool == tool and f.times > 0), None)
        if failure:
            failure.times -= 1
        if failure and not failure.applied:
            return error(failure.message)
        tab = self.tabs.get((args.get("file_id"), args.get("sheet_id")))
        if tab is None:
            return error("code:404001, msg:sheet not found")
        handler = getattr(tab, tool.removeprefix("sheet."), None)
        if not tool.startswith("sheet.") or handler is None:
            return error(f"code:400000, msg:tool {tool} not handled by the fake")
        try:
            result = handler(args)
        except FakeRefusal as e:
            return error(str(e))
        if failure:
            return error(failure.message)
        if tool == "sheet.get_cell_data" and self.links_read_back_as_addresses:
            result = {"cells": [read_back_as_address(c) for c in result["cells"]]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps({"error": "", "trace_id": "t", **result},
                                                            ensure_ascii=False)}]}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


def record_sheet(docs: FakeTencentDocs, file_id: str = FILE_ID, tab_id: str = TAB_ID, *,
                 headers: tuple[str, ...] = HEADERS, spans=SPANS, rows: int = 2) -> FakeTab:
    """A tab shaped like a subtitle group's sheet: the headers in the first row, a row with a
    link to the highlight workbook in the second, then `rows` rows people typed (oldest
    last), every row merged in `spans`."""
    tab = docs.tab(file_id, tab_id)
    tab.fill_row(0, list(headers), spans)
    tab.fill_row(1, ["路灯表"], spans)
    for i in range(rows):
        tab.fill_row(2 + i, [f"主播{i + 1}", f"2026/10/{20 - i}", "杂谈", f"旧的直播{i + 1}"], spans)
    return tab


def settings() -> TencentSettings:
    return TencentSettings(token=TOKEN, file_id="", retry_delay=0)


@dataclass
class Setup:
    keeper: SourceRecordKeeper
    docs: FakeTencentDocs
    tab: FakeTab
    sheet: SheetTab

    def settle(self) -> None:
        """Run the worker until nothing more can be written now."""
        asyncio.run(settle(self.keeper))


async def settle(keeper: SourceRecordKeeper) -> None:
    while await keeper.write_next():
        pass


_DATA = tempfile.TemporaryDirectory(prefix="source-record-")  # removed when the test run ends


def no_groups() -> QQGroups:
    """An empty groups store in a fresh folder of its own (the keeper's state files go beside it)."""
    return QQGroups(Path(tempfile.mkdtemp(dir=_DATA.name)) / "qq_groups.json")


def make(docs: FakeTencentDocs | None = None, **kw: Any) -> Setup:
    """A keeper writing through a fake endpoint that holds one record sheet tab (FILE_ID, TAB_ID).
    Without `groups`, an empty groups store of its own: no group records anybody."""
    docs = docs or FakeTencentDocs()
    tab = docs.tabs.get((FILE_ID, TAB_ID)) or record_sheet(docs)
    kw.setdefault("groups", no_groups())
    keeper = SourceRecordKeeper(settings(), client=docs.client(), **kw)
    return Setup(keeper, docs, tab, SheetTab(FILE_ID, TAB_ID))
