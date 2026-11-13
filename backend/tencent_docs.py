"""Upload a 路灯表 to Tencent Docs (docs.qq.com) as a new sub-sheet of an existing spreadsheet.

    settings = load_credentials(Path("config.json"), file_id=parse_file_id(link))
    url = upload_table(table, settings, sheet_title=sheet_title(table, "20261111"))

Goes through Tencent Docs' official MCP endpoint (https://docs.qq.com/openapi/mcp) with a
personal token: scan a QR code at https://docs.qq.com/open/auth/mcp.html (no developer
registration). The target spreadsheet is per streamer (the dashboard's gear, see
backend/monitor/roster.py); the token is shared and stays in .env. config.json only names
the env var:

    "upload": { "tencent": { "token_env": "TENCENT_DOCS_MCP_TOKEN" } }

MCP facts this relies on (checked against the live endpoint, 2026-11-12):
- JSON-RPC 2.0 over HTTP POST, header `Authorization: <token>`; `tools/call` works without
  `initialize`. A bad or expired token answers HTTP 401 `{"error": "invalid_token"}`.
- A tool failure answers HTTP 200 with a JSON-RPC `error` whose message carries the business
  code ("code:608651, msg:..."). A success's text content is JSON with `"error": ""`.
- sheet.add_sheet takes `index` (0 = first tab) and returns `sheet_id`; a new sheet has 200
  rows. sheet.set_cell_style / set_dimension_size (pixels) / set_freeze give the same look
  as the Excel download.
- Daily quota (MCP overview page): 2000 calls for a free account. One upload is about
  8 calls + 1 per run of same-coloured rows (~50 for a 5-hour stream).

The newest stream becomes the first tab. Uploading the same title again replaces that
sheet (unless it is the spreadsheet's only one).

By hand: `uv run -m backend.tencent_docs <.highlights.json or archive folder> --sheet <link or id> [--title T]`
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from backend.archive import Archive, Artifact, file_date, read_file
from backend.sheet import (HEAD_COLOR, HEAD_FONT_COLOR, HEADER, HIGH_CHAT_COLOR, HOT_COLOR, WIDTHS, cells,
                           score_style)

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_URL = "https://docs.qq.com/openapi/mcp"
TOKEN_PAGE = "https://docs.qq.com/open/auth/mcp.html"
SHEET_URL = "https://docs.qq.com/sheet/{file_id}?tab={sheet_id}"
TOKEN_ENV = "TENCENT_DOCS_MCP_TOKEN"

NEW_SHEET_ROWS = 200  # rows Tencent gives a new sheet
ROW_HEADROOM = 20  # extra rows when a long stream needs more
ROWS_PER_WRITE = 50  # rows per set_range_value; topic logs make rows long, keep requests small
PX_PER_CHAR = 7.5  # Excel width units (backend.sheet.WIDTHS) -> pixels
MAX_TITLE = 31  # sheet.add_sheet's documented limit
BAD_TITLE_CHARS = re.compile(r'[\[\]:*?/\\]')  # not allowed in Excel sheet names (kept for exports)
LAST_COL = len(HEADER) - 1


class TencentDocsError(Exception):
    """Bad settings, or Tencent Docs refused a request (`code`: HTTP status or business code)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class TencentSettings:
    token: str = field(repr=False)
    file_id: str
    timeout: float = 60
    retries: int = 2  # extra tries for a timed-out call (never for creating or deleting a sheet)
    retry_delay: float = 3


def load_credentials(config: Path, file_id: str = "",
                     env: Mapping[str, str] | None = None) -> TencentSettings:
    """The MCP token from the environment (.env), under the name config.json's
    `upload.tencent.token_env` gives (default TENCENT_DOCS_MCP_TOKEN). `env` replaces
    os.environ + .env (tests). A missing token raises TencentDocsError naming it."""
    if env is None:
        load_dotenv(REPO_ROOT / ".env")
        env = os.environ
    cfg = (json.loads(config.read_text(encoding="utf-8")).get("upload") or {}).get("tencent") or {}
    name = cfg.get("token_env") or TOKEN_ENV
    token = (env.get(name) or "").strip()
    if not token:
        raise TencentDocsError(f"Tencent Docs upload needs {name} in .env (a personal token from {TOKEN_PAGE})")
    return TencentSettings(token=token, file_id=file_id, timeout=float(cfg.get("timeout", 60)))


def parse_file_id(text: str) -> str | None:
    """The spreadsheet id from a docs.qq.com/sheet/<id> link, or a bare id; None if neither."""
    text = (text or "").strip()
    if m := re.fullmatch(r"(?:https?://)?docs\.qq\.com/sheet/([A-Za-z0-9]+)(?:[/?#].*)?", text):
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9]{6,}", text) else None


def sheet_title(table: dict[str, Any], date: str = "") -> str:
    """`<YYYYMMDD> <creator> <title>`, made safe for a sheet name and cut to MAX_TITLE."""
    text = " ".join(p for p in (date, table.get("streamer") or "", table.get("title") or "") if p)
    text = re.sub(r"\s+", " ", BAD_TITLE_CHARS.sub("_", text)).strip()
    return text[:MAX_TITLE].rstrip() or "路灯表"


def unique_title(title: str, taken: set[str]) -> str:
    """`title`, or `title (2)`, `(3)`, ... when taken."""
    title = title[:MAX_TITLE]
    n = 1
    candidate = title
    while candidate in taken:
        n += 1
        suffix = f" ({n})"
        candidate = title[:MAX_TITLE - len(suffix)].rstrip() + suffix
    return candidate


def table_grid(table: dict[str, Any]) -> list[list[Any]]:
    """Header + one line per segment: the same cell text as the Excel download."""
    return [HEADER, *(cells(r) for r in table.get("rows") or [])]


def cell_value(row: int, col: int, value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"row": row, "col": col, "value_type": "NUMBER", "number_value": value}
    return {"row": row, "col": col, "value_type": "STRING", "string_value": str(value)}


def cell_style(rows: list[dict[str, Any]], r: int, c: int) -> dict[str, Any]:
    """The complete look of sheet cell (r, c), as in the Excel download. Complete, because
    set_cell_style replaces a cell's whole style: anything left out goes back to default."""
    argb = "FF{}".format
    h_align = "general" if c == LAST_COL else "left" if c in (3, 4) else "center"  # 剪辑 free; long text left
    if r == 0:
        return {"bold": True, "bg_color": argb(HEAD_COLOR), "font_color": argb(HEAD_FONT_COLOR),
                "horizontal_align": "general" if c == LAST_COL else "center", "vertical_align": "center"}
    row = rows[r - 1]
    style: dict[str, Any] = {"horizontal_align": h_align, "vertical_align": "center", "wrap_text": True}
    if row.get("top_pick"):
        style["bg_color"] = argb(HOT_COLOR)
    if c == 5 and (s := score_style(row.get("score"))):
        style |= {"font_color": argb(s[0]), "bold": s[1]}
    if c == 6 and row.get("chat_level") == "高":
        style |= {"font_color": argb(HIGH_CHAT_COLOR), "bold": True}
    return style


def sheet_styles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """set_cell_style calls giving every cell its complete look, each cell set exactly once.
    Neighbouring cells that look the same share a call: runs of rows down each column, and
    columns whose runs are identical (开始 / 结束 / 话题分类, the two long-text columns) together."""
    def runs(c: int) -> list[tuple[int, int, tuple]]:
        out, r = [], 0
        for key, group in itertools.groupby(range(len(rows) + 1),
                                            key=lambda i: tuple(sorted(cell_style(rows, i, c).items()))):
            n = len(list(group))
            out.append((r, r + n - 1, key))
            r += n
        return out

    styles: list[dict[str, Any]] = []
    columns = [(c, runs(c)) for c in range(LAST_COL + 1)]
    for col_runs, group in itertools.groupby(columns, key=lambda x: x[1]):
        cols = [c for c, _ in group]
        for first, last, key in col_runs:
            styles.append({"start_row": first, "end_row": last, "start_col": cols[0], "end_col": cols[-1],
                           **dict(key)})
    return styles


class _Mcp:
    """Tool calls on one spreadsheet through the Tencent Docs MCP endpoint."""

    def __init__(self, settings: TencentSettings, client: httpx.Client):
        self.s, self.client, self.n = settings, client, 0

    def call(self, tool: str, retry: bool = True, **args: Any) -> dict[str, Any]:
        self.n += 1
        body = {"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                "params": {"name": tool, "arguments": {"file_id": self.s.file_id, **args}}}
        headers = {"Authorization": self.s.token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        tries = 1 + (self.s.retries if retry else 0)
        for attempt in range(tries):
            try:
                resp = self.client.post(MCP_URL, headers=headers, json=body, timeout=self.s.timeout)
            except httpx.TimeoutException:
                if attempt + 1 < tries:
                    time.sleep(self.s.retry_delay)
                    continue
                raise TencentDocsError(f"Tencent Docs did not answer in time ({tool})") from None
            except httpx.HTTPError as e:
                raise TencentDocsError(f"Tencent Docs request failed: {type(e).__name__}") from None
            if resp.status_code in (401, 403):
                raise TencentDocsError(
                    f"Tencent Docs rejected the token (HTTP {resp.status_code}, invalid or expired): "
                    f"get a new one at {TOKEN_PAGE} and update {TOKEN_ENV} in .env", resp.status_code)
            data = _json(resp)
            if err := data.get("error"):
                message = str(err.get("message") if isinstance(err, dict) else err)
                if "timeout" in message.lower() and attempt + 1 < tries:
                    time.sleep(self.s.retry_delay)  # the backend now and then times out internally
                    continue
                m = re.search(r"code:\s*(-?\d+)", message)
                raise TencentDocsError(f"Tencent Docs {tool} failed: {message}", int(m.group(1)) if m else None)
            result = data.get("result") or {}
            text = "".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
            try:
                out = json.loads(text) if text else {}
            except ValueError:
                out = {"text": text}
            if result.get("isError") or (isinstance(out, dict) and out.get("error")):
                raise TencentDocsError(f"Tencent Docs {tool} failed: {out.get('error') or text}")
            return out if isinstance(out, dict) else {"result": out}
        raise AssertionError("unreachable")


def _json(resp: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message of a response, plain JSON or server-sent events."""
    text = resp.text
    if "text/event-stream" in resp.headers.get("content-type", ""):
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data[-1] if data else ""
    try:
        return json.loads(text)
    except ValueError:
        raise TencentDocsError(f"Tencent Docs answered HTTP {resp.status_code} without JSON") from None


def upload_table(table: dict[str, Any], settings: TencentSettings, *, sheet_title: str,
                 client: httpx.Client | None = None) -> str:
    """Add the 路灯表 as the first sheet of settings.file_id, formatted like the Excel
    download; returns its `?tab=` URL.

    A sheet with the same title (an earlier upload) is deleted first; only when it is the
    spreadsheet's only sheet does the new one get ` (2)` instead. If filling the new sheet
    fails, it is deleted again (best effort) before the error is raised.
    """
    rows = table.get("rows") or []
    grid = table_grid(table)
    own = client is None
    client = client or httpx.Client()
    try:
        api = _Mcp(settings, client)
        sheets = api.call("sheet.get_sheet_info").get("sheets") or []
        title = sheet_title[:MAX_TITLE]
        old = [s for s in sheets if s.get("sheet_name") == title]
        if old and len(sheets) > len(old):
            for s in old:
                api.call("sheet.delete_sheet", retry=False, sheet_id=s["sheet_id"])
        else:
            title = unique_title(title, {s.get("sheet_name") for s in sheets})
        sheet_id = api.call("sheet.add_sheet", retry=False, name=title, index=0).get("sheet_id")
        if not sheet_id:
            raise TencentDocsError(f"Tencent Docs created sheet '{title}' but returned no sheet_id")
        try:
            if len(grid) > NEW_SHEET_ROWS:
                api.call("sheet.insert_dimension", sheet_id=sheet_id, dimension_type="row",
                         index=NEW_SHEET_ROWS - 1, direction="after",
                         count=len(grid) - NEW_SHEET_ROWS + ROW_HEADROOM)
            for start in range(0, len(grid), ROWS_PER_WRITE):
                values = [v for r, line in enumerate(grid[start:start + ROWS_PER_WRITE], start)
                          for c, x in enumerate(line) if (v := cell_value(r, c, x))]
                api.call("sheet.set_range_value", sheet_id=sheet_id, values=values)
            for style in sheet_styles(rows):
                api.call("sheet.set_cell_style", sheet_id=sheet_id, **style)
            api.call("sheet.set_dimension_size", sheet_id=sheet_id, dimensions=[
                {"dimension_type": "col", "index": i, "size": round(w * PX_PER_CHAR)} for i, w in enumerate(WIDTHS)])
            api.call("sheet.set_freeze", sheet_id=sheet_id, row_count=1, col_count=0)
        except TencentDocsError:
            try:
                api.call("sheet.delete_sheet", retry=False, sheet_id=sheet_id)
            except TencentDocsError:
                pass
            raise
        return SHEET_URL.format(file_id=settings.file_id, sheet_id=sheet_id)
    finally:
        if own:
            client.close()


def find_table(path: Path) -> Path:
    """The highlight table file itself, or the archive's highlight table when `path` is an archive folder."""
    if path.is_dir():
        found = Archive.from_folder(path).file(Artifact.HIGHLIGHTS)
        if not found:
            raise TencentDocsError(f"no *{Artifact.HIGHLIGHTS.suffixes[0]} in {path}")
        return found
    if not path.is_file():
        raise TencentDocsError(f"not found: {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    """uv run -m backend.tencent_docs <.highlights.json | archive folder> --sheet <link or id> [--title T]"""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # sheet titles on a Windows console
    p = argparse.ArgumentParser(prog="backend.tencent_docs",
                                description="Upload a 路灯表 as a new sheet of a Tencent Docs spreadsheet.")
    p.add_argument("path", type=Path, help="<stem>.highlights.json, or the archive folder holding it")
    p.add_argument("--sheet", required=True, help="target spreadsheet: its docs.qq.com/sheet/... link, or its id")
    p.add_argument("--title", help="sheet title (default: '<YYYYMMDD> <creator> <title>')")
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    args = p.parse_args(argv)
    try:
        file_id = parse_file_id(args.sheet)
        if not file_id:
            raise TencentDocsError(f"not a Tencent Docs spreadsheet link or id: {args.sheet}")
        settings = dataclasses.replace(load_credentials(args.config), file_id=file_id)
        file = find_table(args.path)
        table = read_file(file, Artifact.HIGHLIGHTS)
        url = upload_table(table, settings, sheet_title=args.title or sheet_title(table, file_date(file)))
    except (TencentDocsError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Done: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
