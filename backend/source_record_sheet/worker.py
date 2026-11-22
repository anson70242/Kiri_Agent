"""The source record sheet's worker: is told about lives (`processed`, returns at once),
asks the QQ groups store which groups' sheets record the streamer, and writes one row per
tab (`run`), one row at a time in the order told, so that two rows never race for the third
row.

Everything outside comes in from the caller: the Tencent Docs settings (the token; the file
id comes with each tab), the QQ groups store and the HTTP client.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from backend.tencent_docs import SheetTabCalls, TencentDocsError, TencentSettings

from .row import RowKind, SheetRow
from .writing import MissingHeader, write_row

if TYPE_CHECKING:
    from backend.monitor import Streamer
    from backend.notify.qq import QQGroups

log = logging.getLogger(__name__)

IDLE_CHECK = 300.0  # seconds the loop sleeps without being woken, before it looks again


@dataclass(frozen=True)
class SheetTab:
    """One tab of a Tencent Docs spreadsheet: where a group's rows go."""

    file_id: str
    tab_id: str


@dataclass
class RowJob:
    """One row waiting to be written into one tab."""

    tab: SheetTab
    row: SheetRow
    group: str = ""  # the name of the group (or groups) whose sheet this tab is: for the log


class SourceRecordKeeper:
    """`groups`: the QQ groups store, asked which groups record a streamer; None: no group
    does, and a telling writes nothing."""

    def __init__(self, settings: TencentSettings, *, groups: QQGroups | None = None,
                 client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.groups = groups
        self.client = client or httpx.Client()
        self._jobs: list[RowJob] = []
        self._wake = asyncio.Event()

    def processed(self, streamer: Streamer, *, date: str, title: str, link: str, expires: float | None,
                  stream_id: str) -> None:
        """This live is fully processed ("全部处理完成"), here is its short download link: a
        录播 row for every tab that records the streamer. `date` is the archive's (`YYYYMMDD`),
        `expires` when the link stops working (None: it does not). Returns at once."""
        self._tell(streamer, SheetRow(RowKind.ARCHIVE, streamer.name, date, title, link, expires, stream_id))

    def _tell(self, streamer: Streamer, row: SheetRow) -> None:
        """One job per tab that records the streamer: two groups naming one tab share it."""
        tabs: dict[SheetTab, list[str]] = {}
        for group in self.groups.recording(streamer) if self.groups else []:
            tabs.setdefault(SheetTab(group.sheet.file_id, group.sheet.tab_id), []).append(group.name)
        for tab, names in tabs.items():
            self.add(tab, row, group="、".join(names))

    def add(self, tab: SheetTab, row: SheetRow, group: str = "") -> None:
        """A row to write into `tab` (the sheet of `group`). Returns at once; the worker writes it."""
        self._jobs.append(RowJob(tab, row, group))
        self._wake.set()

    async def run(self) -> None:
        """Write rows forever (cancel to stop)."""
        while True:
            self._wake.clear()
            try:
                while await self.write_next():
                    pass
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("source record sheet worker error")
            try:
                await asyncio.wait_for(self._wake.wait(), IDLE_CHECK)
            except TimeoutError:
                pass

    async def write_next(self) -> bool:
        """Write the oldest job's row; False when there was none. One try: the job ends either way."""
        if not self._jobs:
            return False
        job = self._jobs.pop(0)
        calls = SheetTabCalls(dataclasses.replace(self.settings, file_id=job.tab.file_id), job.tab.tab_id, self.client)
        row = job.row
        live = f"{job.group}: {row.streamer} - {row.title}" if job.group else f"{row.streamer} - {row.title}"
        try:
            await asyncio.to_thread(write_row, calls, row)
        except (MissingHeader, TencentDocsError) as e:
            log.warning("source record sheet: %s row not written for %s: %s", row.kind, live, e)
            return True
        log.info("source record sheet: %s row written for %s", row.kind, live)
        return True

    def close(self) -> None:
        self.client.close()
