"""The group-files module's worker: takes posted files in from the gateway (`posted`), and
runs two loops (`run`): one downloads the jobs' files, one at a time in the order posted,
starting at once; the other uploads them, one at a time in the order posted, in one turn at
the drive for every file that is ready by the time the one before it ends. A job ends when
its file is on the drive, when the same file is there already, when it is given up on, or
when its group's switch is off as its download or its turn at the drive comes (cancelled);
its local copy goes with it.

A failed attempt is tried again later (failures.py): the loops wake when the next job is
due. While another upload has the drive, a third loop (`tell_waiting`) looks for downloaded
files that have waited a minute and tells their groups they are queued, once per file. The
jobs file is runtime state that survives a restart: at start an upload mark left by a kill
is cleared (that upload is done again from the local file), and temporary files that belong
to no job are removed. A download that broke off continues from its `.part`.

Everything outside comes in from the caller: the groups store (the switches and the group
folder; the module's state files sit beside its file), the drive (`turn`, then
`folder_names` and `put_file` in it; `uploading`: the Quark module), the HTTP client for
QQ's file host, the clock, and the folder for local copies (on the downloads volume).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from backend.archive import DOWNLOADING_SUFFIX
from backend.downloader.auto import MAX_ERRORS, RETRY_INTERVAL
from backend.drive import DriveAuthorizationLapsed, DriveProgress
from backend.quark import RunningUpload, UploadKind

from .download import AddressRefused, fetch
from .failures import after_failure
from .jobs import GroupFileJob, GroupFileJobs, next_due, next_to_download, next_to_upload, state_files
from .naming import drive_name, file_name
from .record import GroupFileUploads, Upload
from .report import GroupFileReport
from .rules import filing_subfolder

if TYPE_CHECKING:
    from backend.notify.qq import QQGroups

log = logging.getLogger(__name__)

IDLE_CHECK = 300.0  # seconds a loop sleeps without being woken, before it looks again
# A downloaded file that has waited this long (seconds since its download ended) for another
# upload to end is told to its group as waiting: files that only queue behind a small one
# stay silent, and the message is still a reply to the member's.
WAITING_AFTER = 60.0
WAITING_CHECK = 10.0  # seconds between two looks for such files, while an upload has the drive


class DriveCalls(Protocol):
    """What the module asks of the drive in its turn (backend.quark.HeldDrive)."""

    def folder_names(self, folder: str) -> list[str]: ...

    def put_file(self, local: Path, folder: str, progress: DriveProgress | None = None) -> str: ...


class GroupFileDrive(Protocol):
    """The drive the module is given (backend.quark.Quark): a turn at it, and what has it now."""

    def turn(self, kind: UploadKind) -> AbstractAsyncContextManager[DriveCalls]: ...

    @property
    def uploading(self) -> RunningUpload | None: ...


class Doing(StrEnum):
    """What the worker does with a job: each has its loop; the queue box's rows name it."""

    DOWNLOAD = "download"
    UPLOAD = "upload"


def message_time(timestamp: Any, clock: Callable[[], float]) -> float:
    """A message's `timestamp` (ISO 8601, as QQ sends it) in epoch seconds; the clock's time
    when it is missing or cannot be read."""
    try:
        return datetime.fromisoformat(str(timestamp)).timestamp()
    except ValueError:
        return clock()


class GroupFileKeeper:
    def __init__(self, groups: QQGroups, drive: GroupFileDrive | None, *, temp: Path,
                 client: httpx.AsyncClient | None = None, clock: Callable[[], float] = time.time,
                 report: GroupFileReport | None = None,
                 retry_interval: float = RETRY_INTERVAL, max_errors: int = MAX_ERRORS) -> None:
        self.groups = groups
        self.drive = drive  # None: the drive is not available, posted files are ignored
        jobs_file, uploads_file = state_files(groups.path)  # beside the groups file
        self.jobs = GroupFileJobs(jobs_file)
        self.uploads = GroupFileUploads(uploads_file)  # the record of uploads
        self.temp = temp  # one folder per job in here, named after its id
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20))
        self.clock = clock
        self.report = report or GroupFileReport()
        # The automatic download's (download.auto.retry_minutes, max_errors): the server
        # passes the pipeline's, so the module has no settings of its own.
        self.retry_interval = retry_interval
        self.max_errors = max_errors
        self._warned = False  # "the drive is not available" was logged
        self._wake = {Doing.DOWNLOAD: asyncio.Event(), Doing.UPLOAD: asyncio.Event()}
        self._active: dict[Doing, dict[str, Any] | None] = {Doing.DOWNLOAD: None, Doing.UPLOAD: None}
        self._in_hand: GroupFileJob | None = None  # the job being uploaded, in the worker's turn at the drive

    # ---- taking a file in ----

    def posted(self, group: str, msg_id: str, timestamp: Any, attachment: dict[str, Any]) -> None:
        """The gateway hands over a `file` attachment of a message in `group` (its openid), with
        the message's id and `timestamp`. It becomes a saved job when the group's files go to
        the drive, a filing rule keeps it and the drive is available; else it is dropped."""
        files = self.groups.group_files(group)
        if files is None or not files.to_drive or not files.folder:
            return
        name = str(attachment.get("filename") or "")
        subfolder = filing_subfolder(name)
        if subfolder is None:
            return
        if self.drive is None:
            if not self._warned:
                self._warned = True
                log.warning("group files are not kept: the drive is not available on this server")
            return
        try:
            size = int(attachment["size"])
        except (KeyError, TypeError, ValueError):
            log.warning("group file %s/%s not kept: QQ gave no size", files.folder, name)
            return
        job = GroupFileJob(group, files.folder, subfolder, name, size, str(attachment.get("url") or ""),
                           str(msg_id or ""), message_time(timestamp, self.clock))
        self.jobs.add(job)
        log.info("group file queued: %s/%s", job.folder, name)
        self._wake[Doing.DOWNLOAD].set()

    # ---- the worker ----

    async def run(self) -> None:
        """Pick up what a restart left, then download and upload forever (cancel to stop)."""
        try:
            self._start()
        except Exception:  # noqa: BLE001 - the loops still run; a leftover only takes room
            log.exception("group files: the start's clean-up failed")
        await asyncio.gather(self._loop(Doing.DOWNLOAD, self.download_next), self._loop(Doing.UPLOAD, self.upload_next),
                             self._watch())

    def _start(self) -> None:
        """An upload marked as running was cut off by a kill: the mark goes and the upload is
        done again from the local file. A downloaded job whose local file is gone is
        downloaded again. Temporary files that belong to no job are removed."""
        jobs = self.jobs.load()
        for job in jobs:
            gone = job.downloaded and self._downloaded_file(job) is None
            if job.uploading or gone:
                job.uploading = False
                if gone:
                    job.downloaded, job.downloaded_at = False, None
                self.jobs.replace(job)
        if self.temp.is_dir():
            ids = {j.id for j in jobs}
            for leftover in self.temp.iterdir():
                if leftover.name not in ids:
                    log.info("group files: removing a leftover %s", leftover.name)
                    if leftover.is_dir():
                        shutil.rmtree(leftover, ignore_errors=True)
                    else:
                        leftover.unlink(missing_ok=True)

    async def _loop(self, doing: Doing, step: Callable[[], Awaitable[bool]]) -> None:
        wake = self._wake[doing]
        while True:
            wake.clear()
            try:
                while await step():
                    pass
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("group file %s worker error", doing)
            try:
                await asyncio.wait_for(wake.wait(), self._sleep(doing))
            except TimeoutError:
                pass

    def _sleep(self, doing: Doing) -> float:
        """How long a loop may sleep: until its next job waiting for a retry is due."""
        try:
            due = next_due(self.jobs.load(), downloaded=doing is Doing.UPLOAD)
        except Exception:  # noqa: BLE001 - keep the worker alive
            log.exception("group file %s worker error", doing)
            due = None
        return IDLE_CHECK if due is None else min(IDLE_CHECK, max(1.0, due - self.clock()))

    async def download_next(self) -> bool:
        """Download the next job's file; False when there was none."""
        job = next_to_download(self.jobs.load(), self.clock())
        if job is None:
            return False
        if self._switched_off(job):
            return True
        local = self._local(job)
        part = local.with_name(local.name + DOWNLOADING_SUFFIX)  # until it is complete
        row = self._activate(Doing.DOWNLOAD, job)
        try:
            job.sha256 = await fetch(self.client, job.url, job.size, part,
                                     progress=lambda got: row.update(done=got))
            part.replace(local)
        except AddressRefused as e:  # never fetched: trying again cannot help
            await self._failed(job, str(e), final=True)
            return True
        except Exception as e:  # noqa: BLE001 - any failure is a failed attempt
            await self._failed(job, str(e) or type(e).__name__)
            return True
        finally:
            self._active[Doing.DOWNLOAD] = None
        job.downloaded, job.downloaded_at = True, self.clock()
        self.jobs.replace(job)
        self._wake[Doing.UPLOAD].set()
        return True

    async def upload_next(self) -> bool:
        """Upload the downloaded jobs' files in one turn at the drive, in the order posted;
        False when there was none. Every file downloaded (and due) by the time the one
        before it ends goes in the same turn, so group files go before a drive copy that
        waits; a file still downloading then does not hold the drive copy back."""
        job = next_to_upload(self.jobs.load(), self.clock())
        if job is None:
            return False
        if self.drive is None:
            await self._upload(job, None)
            return True
        async with self.drive.turn(UploadKind.GROUP_FILE) as drive:
            # Looked at again now that the turn came: a job may have ended while it waited.
            while (job := next_to_upload(self.jobs.load(), self.clock())) is not None:
                await self._upload(job, drive)
        return True

    async def _upload(self, job: GroupFileJob, drive: DriveCalls | None) -> None:
        """Upload one job's file, in the worker's turn at the drive. Its group's switch is
        looked at first: a job switched off while it waited for the turn sends nothing. Only
        then is it marked as uploading and shown as the upload in the queue box."""
        if self._switched_off(job):
            return
        job.uploading = True  # the attempt's end clears it
        self.jobs.replace(job)
        row = self._activate(Doing.UPLOAD, job)
        self._in_hand = job
        try:
            if drive is None:
                raise RuntimeError("网盘不可用")
            names = await asyncio.to_thread(drive.folder_names, job.folder)
            choice = drive_name(job, names, self.uploads)
            if not choice.upload:
                self._end(job)
                await self.report.already_there(job, choice.place)
                return
            local = self._local(job)
            if local.name != choice.name:
                local = local.replace(local.with_name(choice.name))

            def progress(sent: int, total: int | None) -> None:
                row.update(done=sent, total=total or row["total"])

            await asyncio.to_thread(drive.put_file, local, job.folder, progress)
        except DriveAuthorizationLapsed as e:
            await self._failed(job, str(e) or "网盘授权已失效", lapsed=True)
            return
        except Exception as e:  # noqa: BLE001 - any failure is a failed attempt
            await self._failed(job, str(e) or type(e).__name__)
            return
        finally:
            self._active[Doing.UPLOAD] = None
            self._in_hand = None
        self.uploads.note(Upload(job.group, choice.place, job.size, job.sha256, self.clock()))
        self._end(job)
        await self.report.uploaded(job, choice.place)

    async def tell_waiting(self) -> None:
        """Tell the group of each file that has waited a minute for another upload to end
        that it is queued behind what has the drive now. Once per file (the job notes it, so
        a restart does not tell it again). A file waiting for its retry or for the
        authorization is not due: its group had the failure message instead. Once its retry
        has come it waits like any other, and the minute counts from then."""
        while (running := self.drive.uploading if self.drive is not None else None) is not None:
            job = self._untold()
            if job is None:
                return
            # Saved from the copy just loaded, before the message: a restart does not tell it again.
            job.waiting_told = True
            self.jobs.replace(job)
            await self.report.waiting(job, running)

    def _untold(self) -> GroupFileJob | None:
        """The first job that has waited a minute for the drive and was not told so."""
        now = self.clock()
        for job in self.jobs.load():
            if (job.waiting_told or not job.downloaded or job.downloaded_at is None or not job.due(now)
                    or now - max(job.downloaded_at, job.next_try) < WAITING_AFTER):
                continue
            # The job in hand has the worker's turn: what has the drive is its own upload.
            if self._in_hand is not None and self._in_hand.id == job.id:
                continue
            return job
        return None

    async def _watch(self) -> None:
        while True:
            try:
                await self.tell_waiting()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("group file waiting watch error")
            await asyncio.sleep(WAITING_CHECK)

    async def _failed(self, job: GroupFileJob, reason: str, lapsed: bool = False, final: bool = False) -> None:
        """After a failed attempt: kept to be tried again, or given up on (failures.py). The
        report is told which, with the job as it is now."""
        after = after_failure(job, reason, lapsed, self.clock(), retry_interval=self.retry_interval,
                              max_errors=self.max_errors, final=final)
        if after.kept:
            self.jobs.replace(after.job)
        else:
            self._end(after.job)
        await self.report.failed(after.job, after.reason, after.failure)

    def _switched_off(self, job: GroupFileJob) -> bool:
        """The job's group no longer sends its files to the drive: the job is cancelled
        before it starts (no message, not an error) and its file deleted."""
        files = self.groups.group_files(job.group)
        if files is not None and files.to_drive:
            return False
        self._end(job)
        log.info("group file cancelled, its group's files no longer go to the drive: %s/%s", job.folder, job.name)
        return True

    def _end(self, job: GroupFileJob) -> None:
        """The job is over: dropped from the jobs file, its local copy deleted."""
        self.jobs.drop(job)
        shutil.rmtree(self.temp / job.id, ignore_errors=True)

    def _local(self, job: GroupFileJob) -> Path:
        """The job's local copy: under the name it was posted with (made safe), or under the
        name an upload that did not get through gave it (tried again from that file)."""
        return self._downloaded_file(job) or self.temp / job.id / file_name(job.name)

    def _downloaded_file(self, job: GroupFileJob) -> Path | None:
        """The complete local file in the job's folder, if there is one."""
        folder = self.temp / job.id
        if not folder.is_dir():
            return None
        return next((p for p in folder.iterdir() if p.is_file() and not p.name.endswith(DOWNLOADING_SUFFIX)), None)

    def _activate(self, doing: Doing, job: GroupFileJob) -> dict[str, Any]:
        row = {"id": job.id, "group": job.group_folder, "file": job.name, "doing": doing,
               "done": 0, "total": job.size}
        self._active[doing] = row
        return row

    # ---- for the queue box ----

    def snapshot(self) -> dict[str, Any]:
        """What is being downloaded or uploaded now, with its progress (`done` of `total`
        bytes), and the jobs waiting, oldest first, with the last error and the time of the
        next try of one that failed ("" and 0 for none). Rows name the group (its folder) and
        the file: for a user who sees everything only."""
        active = [dict(a) for a in self._active.values() if a]
        busy = {a["id"] for a in active}
        queue = [{"id": j.id, "group": j.group_folder, "file": j.name,
                  "waiting_for": Doing.UPLOAD if j.downloaded else Doing.DOWNLOAD,
                  "last_error": j.last_error, "next_try": j.next_try}
                 for j in self.jobs.load() if j.id not in busy]
        return {"active": active, "queue": queue}

    async def aclose(self) -> None:
        await self.client.aclose()
