"""The source record sheet's worker: takes rows in (`add`, returns at once) and writes them
(`run`), one row at a time in the order told, so that two rows never race for the third row.

Everything outside comes in from the caller: the Tencent Docs settings (the token; the file
id comes with each tab) and the HTTP client.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass

import httpx

from backend.tencent_docs import SheetTabCalls, TencentDocsError, TencentSettings

from .row import SheetRow
from .writing import MissingHeader, write_row

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


class SourceRecordKeeper:
    def __init__(self, settings: TencentSettings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client()
        self._jobs: list[RowJob] = []
        self._wake = asyncio.Event()

    def add(self, tab: SheetTab, row: SheetRow) -> None:
        """A row to write into `tab`. Returns at once; the worker writes it."""
        self._jobs.append(RowJob(tab, row))
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
        try:
            await asyncio.to_thread(write_row, calls, row)
        except (MissingHeader, TencentDocsError) as e:
            log.warning("source record sheet: %s row not written for %s - %s: %s", row.kind, row.streamer, row.title, e)
            return True
        log.info("source record sheet: %s row written for %s - %s", row.kind, row.streamer, row.title)
        return True

    def close(self) -> None:
        self.client.close()
