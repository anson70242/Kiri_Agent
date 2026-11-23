"""The source record sheet's worker: is told about lives (`processed`, returns at once),
asks the QQ groups store which groups' sheets record the streamer, saves one job per tab
(jobs.py) and writes them (`run`), one row at a time in the order told, so that two rows
never race for the third row. A row in the record of rows written (record.py), or waiting
already, is not saved again: each row of a live goes into a tab once. The jobs file
survives a restart: the rows waiting are written after it.

A failed try is one error of its job, tried again after the retry interval. A job waiting
for its retry holds back the later jobs of its own tab (a tab's rows go in the order told, so
that its retry still finds the empty row it may have left as the third), not those of other
tabs. At the maximum of errors, or at once for a missing
header, the job is dropped and the owner told once (give_up.py). A try that may have
inserted its row is noted in the job before the insert is sent, so that the next try looks
for that row first (writing.py): one job never fills two rows.

Everything outside comes in from the caller: the Tencent Docs settings (the token; the file
id comes with each tab), the QQ groups store (the module's state files sit beside its file),
the HTTP client, the clock, the operator alert and the pipeline's retry settings.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from backend.downloader.auto import MAX_ERRORS, RETRY_INTERVAL
from backend.tencent_docs import SheetTabCalls, TencentDocsError, TencentSettings

from .give_up import given_up_message, without_addresses
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

    def __init__(self, settings: TencentSettings, *, groups: QQGroups, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time, alert: Callable[[str], Any] | None = None,
                 retry_interval: float = RETRY_INTERVAL, max_errors: int = MAX_ERRORS) -> None:
        self.settings = settings
        self.groups = groups
        jobs_file, rows_file = state_files(groups.path)
        self.jobs = RowJobs(jobs_file)
        self.record = RowRecord(rows_file)  # the rows written
        self.client = client or httpx.Client()
        self.clock = clock
        self.alert = alert  # the operator alert (LiveNotifier.alert: Discord); None: the log only
        # The automatic download's (download.auto.retry_minutes, max_errors): the server
        # passes the pipeline's, so the module has no settings of its own.
        self.retry_interval = retry_interval
        self.max_errors = max_errors
        self._wake = asyncio.Event()

    def processed(self, streamer: Streamer, *, date: str, title: str, link: str, expires: float | None,
                  stream_id: str, flash: bool = False) -> None:
        """This live is fully processed ("全部处理完成"), here is the link its message gave: a
        录播 row for every tab that records the streamer. `flash`: the link is its flash link
        (behind 闪传链接), else its short download link (behind 全部下载). `date` is the
        archive's (`YYYYMMDD`), `expires` when the link stops working (None: it does not).
        A live has one 录播 row a tab: told again with the other link, it gets no second.
        Returns at once."""
        self._tell(streamer, SheetRow(RowKind.ARCHIVE, streamer.name, date, title, link, expires, stream_id, flash))

    def uploaded(self, streamer: Streamer, *, date: str, title: str, link: str, stream_id: str) -> None:
        """This live's drive copy is uploaded ("网盘上传完成"), here is its drive link (with its
        extraction code inside): a 网盘 row for every tab that records the streamer. `date` is
        the archive's (`YYYYMMDD`). Returns at once."""
        self._tell(streamer, SheetRow(RowKind.DRIVE_COPY, streamer.name, date, title, link, None, stream_id))

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
                await asyncio.wait_for(self._wake.wait(), self._sleep())
            except TimeoutError:
                pass

    def _sleep(self) -> float:
        """How long the loop may sleep: until the next job first of its tab is due (the jobs
        behind it wait for it, whatever their own time)."""
        try:
            due = min((j.next_try for j in heads(self.jobs.load())), default=None)
        except Exception:  # noqa: BLE001 - keep the worker alive
            log.exception("source record sheet worker error")
            due = None
        return IDLE_CHECK if due is None else min(IDLE_CHECK, max(1.0, due - self.clock()))

    async def write_next(self) -> bool:
        """Write the row of the oldest job that is due and first of its tab; False when there
        was none. A failed try keeps the job for a later one, until the last (see `_failed`)."""
        now = self.clock()
        job = next((j for j in heads(self.jobs.load()) if j.due(now)), None)
        if job is None:
            return False
        if self.record.written(job.key):  # written before a stop that came before the job was dropped
            self.jobs.drop(job)
            return True
        calls = SheetTabCalls(dataclasses.replace(self.settings, file_id=job.tab.file_id), job.tab.tab_id, self.client)
        row = job.row
        live = f"{job.group}: {row.streamer} - {row.title}" if job.group else f"{row.streamer} - {row.title}"
        def inserting() -> None:
            """Saved before the insert is sent: whatever its answer, a later try looks first."""
            if not job.inserted:
                job.inserted = True
                self.jobs.replace(job)

        try:
            await asyncio.to_thread(write_row, calls, row, again=job.inserted, inserting=inserting)
        except Exception as e:  # noqa: BLE001 - any failure is a failed try
            reason = str(e) or type(e).__name__  # the owner's (Chinese for a missing header)
            # The log's: in English, and without an address Tencent Docs' reason may echo.
            logged = (f"the tab's first row has no header {e.header}" if isinstance(e, MissingHeader)
                      else without_addresses(reason))
            log.warning("source record sheet: %s row not written for %s: %s", row.kind, live, logged)
            self._failed(job, reason, final=isinstance(e, MissingHeader),
                         denied=isinstance(e, TencentDocsError) and e.denied)
            return True
        self.record.note(job.key)  # first: a stop between the two leaves a job the record skips
        self.jobs.drop(job)
        log.info("source record sheet: %s row written for %s", row.kind, live)
        return True

    def _failed(self, job: RowJob, reason: str, *, final: bool = False, denied: bool = False) -> None:
        """A failed try: one error of the job, tried again after the retry interval; given up
        on (dropped) at the maximum of errors, or at once when trying again cannot help
        (`final`: a missing header), and the owner told once. `denied`: the token's account
        may not edit the sheet."""
        job = dataclasses.replace(job, errors=job.errors + 1, next_try=self.clock() + self.retry_interval)
        if job.errors < self.max_errors and not final:
            self.jobs.replace(job)
            return
        self.jobs.drop(job)
        if self.alert is None:
            return
        try:
            self.alert(given_up_message(job, reason, denied=denied))
        except Exception:  # noqa: BLE001 - an alert never touches the worker
            log.exception("source record sheet: the owner was not told of a row given up on")

    def close(self) -> None:
        self.client.close()


def heads(jobs: list[RowJob]) -> list[RowJob]:
    """The oldest job of each tab, oldest first: the only one of its tab that may be written
    (a tab's rows go in the order told). The later jobs of a tab wait behind it, also while
    it waits for its retry."""
    first: dict[SheetTab, RowJob] = {}
    for job in jobs:
        first.setdefault(job.tab, job)
    return list(first.values())
