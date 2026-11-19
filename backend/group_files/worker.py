"""The group-files module's worker: takes posted files in from the gateway (`posted`), and
runs two loops (`run`): one downloads the jobs' files, one at a time in the order posted,
starting at once; the other uploads them, one at a time in the order posted. A job ends
when its file is on the drive, when its name is taken there, or when it is given up on; its
local copy goes with it.

Everything outside comes in from the caller: the groups store (the switches and the group
folder), the drive (`folder_names`, `put_file`: the Quark module), the HTTP client for QQ's
file host, the clock, and the folder for local copies (on the downloads volume).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from backend.drive import DriveAuthorizationLapsed, DriveProgress

from .download import fetch
from .failures import after_failure
from .jobs import GroupFileJob, GroupFileJobs, next_to_download, next_to_upload
from .naming import drive_name, file_name
from .report import GroupFileReport
from .rules import filing_subfolder

if TYPE_CHECKING:
    from backend.notify.qq import QQGroups

log = logging.getLogger(__name__)

IDLE_CHECK = 300.0  # seconds a loop sleeps without being woken, before it looks again


class GroupFileDrive(Protocol):
    """What the module asks of the drive (backend.quark.Quark has both)."""

    def folder_names(self, folder: str) -> list[str]: ...

    def put_file(self, local: Path, folder: str, progress: DriveProgress | None = None) -> str: ...


def message_time(timestamp: Any, clock: Callable[[], float]) -> float:
    """A message's `timestamp` (ISO 8601, as QQ sends it) in epoch seconds; the clock's time
    when it is missing or cannot be read."""
    try:
        return datetime.fromisoformat(str(timestamp)).timestamp()
    except ValueError:
        return clock()


class GroupFileKeeper:
    def __init__(self, groups: QQGroups, drive: GroupFileDrive | None, *, jobs_file: Path, temp: Path,
                 client: httpx.AsyncClient | None = None, clock: Callable[[], float] = time.time,
                 report: GroupFileReport | None = None) -> None:
        self.groups = groups
        self.drive = drive  # None: the drive is not available, posted files are ignored
        self.jobs = GroupFileJobs(jobs_file)
        self.temp = temp  # one folder per job in here, named after its id
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20))
        self.clock = clock
        self.report = report or GroupFileReport()
        self._warned = False  # "the drive is not available" was logged
        self._wake = {"download": asyncio.Event(), "upload": asyncio.Event()}
        self._active: dict[str, dict[str, Any] | None] = {"download": None, "upload": None}

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
        self._wake["download"].set()

    # ---- the worker ----

    async def run(self) -> None:
        """Download and upload forever (cancel to stop)."""
        await asyncio.gather(self._loop("download", self.download_next), self._loop("upload", self.upload_next))

    async def _loop(self, what: str, step: Callable[[], Awaitable[bool]]) -> None:
        wake = self._wake[what]
        while True:
            wake.clear()
            try:
                while await step():
                    pass
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("group file %s worker error", what)
            try:
                await asyncio.wait_for(wake.wait(), IDLE_CHECK)
            except TimeoutError:
                pass

    async def download_next(self) -> bool:
        """Download the next job's file; False when there was none."""
        job = next_to_download(self.jobs.load())
        if job is None:
            return False
        local = self._local(job)
        part = local.with_name(local.name + ".part")
        row = self._activate("download", job)
        try:
            job.sha256 = await fetch(self.client, job.url, job.size, part,
                                     progress=lambda got: row.update(done=got))
            part.replace(local)
        except Exception as e:  # noqa: BLE001 - any failure is a failed attempt
            await self._failed(job, str(e) or type(e).__name__)
            return True
        finally:
            self._active["download"] = None
        job.downloaded = True
        self.jobs.replace(job)
        self._wake["upload"].set()
        return True

    async def upload_next(self) -> bool:
        """Upload the next downloaded job's file; False when there was none."""
        job = next_to_upload(self.jobs.load())
        if job is None:
            return False
        row = self._activate("upload", job)
        try:
            if self.drive is None:
                raise RuntimeError("网盘不可用")
            names = await asyncio.to_thread(self.drive.folder_names, job.folder)
            choice = drive_name(job, names)
            if not choice.upload:
                self._end(job)
                await self.report.name_taken(job, f"{job.folder}/{choice.name}")
                return True
            local = self._local(job)
            if local.name != choice.name:
                local = local.replace(local.with_name(choice.name))

            def progress(sent: int, total: int | None) -> None:
                row.update(done=sent, total=total or row["total"])

            place = await asyncio.to_thread(self.drive.put_file, local, job.folder, progress)
        except DriveAuthorizationLapsed as e:
            await self._failed(job, str(e) or "网盘授权已失效", lapsed=True)
            return True
        except Exception as e:  # noqa: BLE001 - any failure is a failed attempt
            await self._failed(job, str(e) or type(e).__name__)
            return True
        finally:
            self._active["upload"] = None
        self._end(job)
        await self.report.uploaded(job, place)
        return True

    async def _failed(self, job: GroupFileJob, reason: str, lapsed: bool = False) -> None:
        """After a failed attempt: kept to be tried again, or given up on (failures.py)."""
        kept = after_failure(job, reason, lapsed, self.clock())
        if kept is None:
            self._end(job)
        else:
            self.jobs.replace(kept)
        await self.report.failed(job, reason, given_up=kept is None)

    def _end(self, job: GroupFileJob) -> None:
        """The job is over: dropped from the jobs file, its local copy deleted."""
        self.jobs.drop(job)
        shutil.rmtree(self.temp / job.id, ignore_errors=True)

    def _local(self, job: GroupFileJob) -> Path:
        """The job's local copy, under the name it was posted with (made safe)."""
        return self.temp / job.id / file_name(job.name)

    def _activate(self, what: str, job: GroupFileJob) -> dict[str, Any]:
        row = {"id": job.id, "group": job.group_folder, "file": job.name, "doing": what,
               "done": 0, "total": job.size}
        self._active[what] = row
        return row

    # ---- for the queue box ----

    def snapshot(self) -> dict[str, Any]:
        """What is being downloaded or uploaded now, with its progress (`done` of `total`
        bytes), and the jobs waiting, oldest first. Rows name the group (its folder) and the
        file: for a user who sees everything only."""
        active = [dict(a) for a in self._active.values() if a]
        busy = {a["id"] for a in active}
        queue = [{"id": j.id, "group": j.group_folder, "file": j.name,
                  "waiting_for": "upload" if j.downloaded else "download"}
                 for j in self.jobs.load() if j.id not in busy]
        return {"active": active, "queue": queue}

    async def aclose(self) -> None:
        await self.client.aclose()
