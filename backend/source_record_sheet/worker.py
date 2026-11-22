"""The source record sheet's worker: is told about lives (`processed`, returns at once),
asks the QQ groups store which groups' sheets record the streamer, saves one job per tab
(jobs.py) and writes them (`run`), one row at a time in the order told, so that two rows
never race for the third row. A row in the record of rows written (record.py), or waiting
already, is not saved again: each row of a live goes into a tab once. The jobs file
survives a restart: the rows waiting are written after it.

Everything outside comes in from the caller: the Tencent Docs settings (the token; the file
id comes with each tab), the QQ groups store (the module's state files sit beside its file)
and the HTTP client.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

import httpx

from backend.tencent_docs import SheetTabCalls, TencentDocsError, TencentSettings

from .jobs import RowJob, RowJobs, SheetTab, state_files
from .record import RowRecord
from .row import RowKind, SheetRow
from .writing import MissingHeader, write_row

if TYPE_CHECKING:
    from backend.monitor import Streamer
    from backend.notify.qq import QQGroups

log = logging.getLogger(__name__)

IDLE_CHECK = 300.0  # seconds the loop sleeps without being woken, before it looks again


class SourceRecordKeeper:
    """`groups`: the QQ groups store, asked which groups record a streamer; the jobs file and
    the record of rows written sit beside its file."""

    def __init__(self, settings: TencentSettings, *, groups: QQGroups, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.groups = groups
        jobs_file, rows_file = state_files(groups.path)
        self.jobs = RowJobs(jobs_file)
        self.record = RowRecord(rows_file)  # the rows written
        self.client = client or httpx.Client()
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
        for group in self.groups.recording(streamer):
            tabs.setdefault(SheetTab(group.sheet.file_id, group.sheet.tab_id), []).append(group.name)
        for tab, names in tabs.items():
            self.add(tab, row, group="、".join(names))

    def add(self, tab: SheetTab, row: SheetRow, group: str = "") -> None:
        """A row to write into `tab` (the sheet of `group`), saved before this returns; the
        worker writes it. Nothing when that row of that live is written or waiting already."""
        job = RowJob(tab, row, group)
        if self.record.written(job.key) or not self.jobs.add(job):
            return
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
        jobs = self.jobs.load()
        if not jobs:
            return False
        job = jobs[0]
        if self.record.written(job.key):  # written before a stop that came before the job was dropped
            self.jobs.drop(job)
            return True
        calls = SheetTabCalls(dataclasses.replace(self.settings, file_id=job.tab.file_id), job.tab.tab_id, self.client)
        row = job.row
        live = f"{job.group}: {row.streamer} - {row.title}" if job.group else f"{row.streamer} - {row.title}"
        try:
            await asyncio.to_thread(write_row, calls, row)
        except (MissingHeader, TencentDocsError) as e:
            log.warning("source record sheet: %s row not written for %s: %s", row.kind, live, e)
            self.jobs.drop(job)
            return True
        self.record.note(job.key)  # first: a stop between the two leaves a job the record skips
        self.jobs.drop(job)
        log.info("source record sheet: %s row written for %s", row.kind, live)
        return True

    def close(self) -> None:
        self.client.close()
