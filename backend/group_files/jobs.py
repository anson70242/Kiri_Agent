"""The group-file jobs: one per posted file that a filing rule keeps, saved the moment it is
posted, in the runtime state file `group_file_jobs.json` next to the QQ groups file (a
damaged one is set aside, the owner alerted, and the service goes on empty).

Which job is downloaded next, and which is uploaded next, is decided here
(`next_to_download`, `next_to_upload`): the oldest first, in the order posted.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.state_file import StateFile, expect_list

JOBS_FILE = "group_file_jobs.json"  # in the data directory, beside qq_groups.json


@dataclass
class GroupFileJob:
    group: str  # the group's openid
    group_folder: str  # the group's name when the file was posted: a later rename moves nothing
    subfolder: str  # the filing rule's
    name: str  # the file's name as posted
    size: int  # bytes, as QQ announced
    url: str  # QQ's download address: kept while the job waits, never logged
    msg_id: str  # the member's message (a reply goes to it)
    posted_at: float  # when the message was posted (epoch seconds)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])  # names its temporary folder
    sha256: str = ""  # worked out by the download
    downloaded: bool = False  # the local copy is complete

    @property
    def folder(self) -> str:
        """Where on the drive the file goes: `<群名>/<subfolder>`."""
        return f"{self.group_folder}/{self.subfolder}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GroupFileJob:
        return cls(**d)


class GroupFileJobs:
    """The jobs file. Every change is a load, a change and a save, in the event loop's
    thread: the worker's two loops never write it at the same time."""

    def __init__(self, path: Path) -> None:
        self._file = StateFile(path, default=list, policy="recover",
                               decode=lambda raw: [GroupFileJob.from_dict(d) for d in expect_list(raw, "group-file jobs")],
                               encode=lambda jobs: [asdict(j) for j in jobs])

    @property
    def path(self) -> Path:
        return self._file.path

    def load(self) -> list[GroupFileJob]:
        return self._file.load()

    def add(self, job: GroupFileJob) -> None:
        self._file.save([*self.load(), job])

    def replace(self, job: GroupFileJob) -> None:
        """Save `job` in place of the job of the same id (gone already: nothing)."""
        self._file.save([job if j.id == job.id else j for j in self.load()])

    def drop(self, job: GroupFileJob) -> None:
        self._file.save([j for j in self.load() if j.id != job.id])


def next_to_download(jobs: list[GroupFileJob]) -> GroupFileJob | None:
    """The oldest job whose file is not downloaded yet."""
    return next((j for j in jobs if not j.downloaded), None)


def next_to_upload(jobs: list[GroupFileJob]) -> GroupFileJob | None:
    """The oldest downloaded job: uploads go in the order posted."""
    return next((j for j in jobs if j.downloaded), None)
