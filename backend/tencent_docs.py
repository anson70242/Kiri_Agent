"""Upload a 路灯表 to Tencent Docs (docs.qq.com) as a new sub-sheet of an existing spreadsheet.

    settings = load_credentials(Path("config.json"), file_id=parse_file_id(link))
    url = upload_table(table, settings, sheet_title=sheet_title(table, "20261111"))

The target spreadsheet is per streamer (the dashboard's gear, see backend/monitor/roster.py);
the credentials are shared and stay in .env. config.json only names the env vars:

    "upload": {
      "tencent": {
        "access_token_env": "TENCENT_DOCS_ACCESS_TOKEN",
        "client_id_env": "TENCENT_DOCS_CLIENT_ID",
        "open_id_env": "TENCENT_DOCS_OPEN_ID"
      }
    }

Spreadsheet Open API v3 facts this relies on (tested against the live API in the
tencent_docs_uploader skill, the docs site itself is not crawlable):
- Every call needs the Access-Token / Client-Id / Open-Id headers.
- Failures still answer HTTP 200; the status is the body's `code` (400006: token expired or wrong).
- batchUpdate (slash, not colon) knows only addSheetRequest, deleteSheetRequest,
  updateRangeRequest and deleteDimensionRequest: no formatting, column widths, frozen
  rows, renaming or moving. A new sheet always lands last, so the returned `?tab=` link matters.
- A sheet holds at most 10000 cells (rowCount × columnCount), checked when it is created.

Uploading the same title again replaces that sheet (unless it is the only one).

By hand: `uv run -m backend.tencent_docs <.highlights.json or archive folder> --sheet <link or id> [--title T]`
"""

from __future__ import annotations

import argparse
import dataclasses
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

from backend.sheet import HEADER, cells

REPO_ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://docs.qq.com/openapi/spreadsheet/v3"
SHEET_URL = "https://docs.qq.com/sheet/{file_id}?tab={sheet_id}"
HIGHLIGHTS_SUFFIX = ".highlights.json"

MAX_CELLS = 10000  # addSheetRequest refuses rowCount * columnCount above this
MIN_ROWS, MIN_COLUMNS, ROW_HEADROOM = 200, 26, 20  # the size Tencent gives a new sheet by hand
ROWS_PER_BATCH = 50  # rows per updateRangeRequest; topic logs make rows long, keep requests small
MAX_TITLE = 31  # assumed (Excel's limit, so the doc still exports); the v3 limit is undocumented
BAD_TITLE_CHARS = re.compile(r'[\[\]:*?/\\]')  # not allowed in Excel sheet names
EXPIRED_TOKEN = 400006

ENV_KEYS = {"access_token": "TENCENT_DOCS_ACCESS_TOKEN", "client_id": "TENCENT_DOCS_CLIENT_ID",
            "open_id": "TENCENT_DOCS_OPEN_ID"}


class TencentDocsError(Exception):
    """Bad settings, or the Tencent Docs API refused a request (`code` is its error code)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class TencentSettings:
    access_token: str = field(repr=False)
    client_id: str = field(repr=False)
    open_id: str = field(repr=False)
    file_id: str
    timeout: float = 60
    retries: int = 2  # extra tries for a timed-out read or cell write (never for creating a sheet)
    retry_delay: float = 3

    def headers(self) -> dict[str, str]:
        return {"Access-Token": self.access_token, "Client-Id": self.client_id, "Open-Id": self.open_id}


def load_credentials(config: Path, file_id: str = "",
                     env: Mapping[str, str] | None = None) -> TencentSettings:
    """Credentials from the environment (.env), under the names config.json's
    `upload.tencent` gives. `env` replaces os.environ + .env (tests). Anything missing
    raises TencentDocsError naming it."""
    if env is None:
        load_dotenv(REPO_ROOT / ".env")
        env = os.environ
    cfg = (json.loads(config.read_text(encoding="utf-8")).get("upload") or {}).get("tencent") or {}
    secrets, missing = {}, []
    for key, default in ENV_KEYS.items():
        name = cfg.get(f"{key}_env") or default
        secrets[key] = (env.get(name) or "").strip()
        if not secrets[key]:
            missing.append(f"{name} in .env")
    if missing:
        raise TencentDocsError("Tencent Docs upload needs " + ", ".join(missing))
    return TencentSettings(**secrets, file_id=file_id, timeout=float(cfg.get("timeout", 60)))


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
    """`title`, or `title (2)`, `(3)`, ... when taken: sheet titles can't be renamed later."""
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


def sheet_size(data_rows: int, data_columns: int) -> tuple[int, int]:
    """A roomy new sheet (like one made by hand) shrunk to fit MAX_CELLS."""
    rows, columns = max(data_rows + ROW_HEADROOM, MIN_ROWS), max(data_columns, MIN_COLUMNS)
    if rows * columns > MAX_CELLS:
        columns = max(data_columns, MAX_CELLS // rows)
    if rows * columns > MAX_CELLS:
        rows = max(data_rows, MAX_CELLS // columns)
    return rows, columns


def cell(value: Any) -> dict[str, Any]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"cellValue": {"number": value}}
    return {"cellValue": {"text": "" if value is None else str(value)}}


class _Api:
    def __init__(self, settings: TencentSettings, client: httpx.Client):
        self.s, self.client = settings, client
        self.base = f"{API_BASE}/files/{settings.file_id}"

    def call(self, method: str, url: str, body: dict | None = None, retry: bool = True) -> dict[str, Any]:
        tries = 1 + (self.s.retries if retry else 0)
        for attempt in range(tries):
            try:
                resp = self.client.request(method, url, headers=self.s.headers(), json=body,
                                           timeout=self.s.timeout)
                data = resp.json()
            except httpx.TimeoutException:
                if attempt + 1 < tries:
                    time.sleep(self.s.retry_delay)
                    continue
                raise TencentDocsError("Tencent Docs did not answer in time") from None
            except httpx.HTTPError as e:
                raise TencentDocsError(f"Tencent Docs request failed: {type(e).__name__}") from None
            except ValueError:
                raise TencentDocsError(f"Tencent Docs answered HTTP {resp.status_code} without JSON") from None
            code = data.get("code") or 0
            if not code and resp.is_success:
                return data
            message = str(data.get("message") or f"HTTP {resp.status_code}")
            # the backend now and then times out internally; a retry usually works
            if "timeout" in message.lower() and attempt + 1 < tries:
                time.sleep(self.s.retry_delay)
                continue
            hint = " (access token expired or wrong: get a new one and update .env)" \
                if code == EXPIRED_TOKEN else ""
            raise TencentDocsError(f"Tencent Docs API error {code}: {message}{hint}", code or None)
        raise AssertionError("unreachable")

    def sheets(self) -> list[dict[str, Any]]:
        return self.call("GET", self.base).get("properties") or []

    def batch(self, request: dict[str, Any], retry: bool = True) -> dict[str, Any]:
        return self.call("POST", f"{self.base}/batchUpdate", {"requests": [request]}, retry)


def upload_table(table: dict[str, Any], settings: TencentSettings, *, sheet_title: str,
                 client: httpx.Client | None = None) -> str:
    """Add the 路灯表 as a new sheet (last tab) of settings.file_id; returns its `?tab=` URL.

    A sheet with the same title (an earlier upload) is deleted first; only when it is the
    spreadsheet's only sheet does the new one get ` (2)` instead. If writing the cells
    fails, the half-filled sheet is deleted again (best effort) before the error is raised.
    """
    grid = table_grid(table)
    width = len(HEADER)
    if len(grid) * width > MAX_CELLS:
        raise TencentDocsError(f"路灯表 is {len(grid) * width} cells; a sheet holds at most {MAX_CELLS}")
    own = client is None
    client = client or httpx.Client()
    try:
        api = _Api(settings, client)
        sheets = api.sheets()
        title = sheet_title[:MAX_TITLE]
        old = [s for s in sheets if s.get("title") == title]
        if old and len(sheets) > len(old):
            for s in old:
                api.batch({"deleteSheetRequest": {"sheetId": s["sheetId"]}}, retry=False)
        else:
            title = unique_title(title, {s.get("title") for s in sheets})
        rows, columns = sheet_size(len(grid), width)
        # no retry: a timed-out create may still have made the sheet
        created = api.batch({"addSheetRequest": {"title": title, "rowCount": rows, "columnCount": columns}},
                            retry=False)
        sheet_id = next((p["sheetId"] for r in created.get("responses") or []
                         if (p := (r.get("addSheetResponse") or {}).get("properties") or {}).get("sheetId")),
                        None)
        if not sheet_id:
            raise TencentDocsError(f"Tencent Docs created sheet '{title}' but returned no sheetId")
        try:
            for start in range(0, len(grid), ROWS_PER_BATCH):
                api.batch({"updateRangeRequest": {"sheetId": sheet_id, "gridData": {
                    "startRow": start, "startColumn": 0,
                    "rows": [{"values": [cell(v) for v in line]} for line in grid[start:start + ROWS_PER_BATCH]],
                }}})
        except TencentDocsError:
            try:
                api.batch({"deleteSheetRequest": {"sheetId": sheet_id}}, retry=False)
            except TencentDocsError:
                pass
            raise
        return SHEET_URL.format(file_id=settings.file_id, sheet_id=sheet_id)
    finally:
        if own:
            client.close()


def find_table(path: Path) -> Path:
    """The .highlights.json itself, or the one inside an archive folder."""
    if path.is_dir():
        found = next((f for f in sorted(path.iterdir()) if f.name.endswith(HIGHLIGHTS_SUFFIX)), None)
        if not found:
            raise TencentDocsError(f"no *{HIGHLIGHTS_SUFFIX} in {path}")
        return found
    if not path.is_file():
        raise TencentDocsError(f"not found: {path}")
    return path


def archive_date(file: Path) -> str:
    """YYYYMMDD from downloads/<creator>/<YYYYMMDD>/<title>/, or the `[YYYYMMDD]` file name prefix."""
    if re.fullmatch(r"\d{8}", file.parent.parent.name):
        return file.parent.parent.name
    m = re.match(r"\[(\d{8})\]", file.name)
    return m.group(1) if m else ""


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
        table = json.loads(file.read_text(encoding="utf-8"))
        url = upload_table(table, settings, sheet_title=args.title or sheet_title(table, archive_date(file)))
    except (TencentDocsError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Done: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
