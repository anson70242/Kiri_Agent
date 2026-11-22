"""The rows waiting to be written: one job per row and tab, saved the moment the module is
told, in the runtime state file `source_record_jobs.json` beside the QQ groups file (a
damaged one is set aside, the owner alerted, and the service goes on empty). A job is the
only place a link is kept, and only while it waits.

The worker writes the oldest job first, in the order told; a job leaves the file when its
row is written or given up on.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.state_file import StateFile, expect_list

from .record import ROWS_FILE, RowKey
from .row import RowKind, SheetRow

JOBS_FILE = "source_record_jobs.json"  # in the data directory, beside qq_groups.json


def state_files(groups_file: Path) -> tuple[Path, Path]:
    """The module's two state files, beside the QQ groups file `groups_file`: the jobs file
    and the record of rows written."""
    return groups_file.parent / JOBS_FILE, groups_file.parent / ROWS_FILE


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
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    errors: int = 0  # failed tries
    next_try: float = 0.0  # not tried again before this time (epoch seconds)
    inserted: bool = False  # a try inserted the row and failed after it: the retry looks for that empty row

    @property
    def key(self) -> RowKey:
        """Which row of which live this is, in which tab: each is written once."""
        return RowKey(self.tab.file_id, self.tab.tab_id, self.row.streamer, self.row.stream_id, self.row.kind)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RowJob:
        row = d["row"]
        return cls(**{**d, "tab": SheetTab(**d["tab"]), "row": SheetRow(**{**row, "kind": RowKind(row["kind"])})})


class RowJobs:
    """The jobs file. Every change is a load, a change and a save, under one lock: a telling
    and the worker never write it at the same time."""

    def __init__(self, path: Path) -> None:
        self._file = StateFile(path, default=list, policy="recover",
                               decode=lambda raw: [RowJob.from_dict(d) for d in expect_list(raw, "source record jobs")],
                               encode=lambda jobs: [asdict(j) for j in jobs])
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._file.path

    def load(self) -> list[RowJob]:
        with self._lock:
            return self._file.load()

    def add(self, job: RowJob) -> bool:
        """Save `job` unless a job for the same row is waiting already; whether it was saved."""
        with self._lock:
            jobs = self._file.load()
            if any(j.key == job.key for j in jobs):
                return False
            self._file.save([*jobs, job])
            return True

    def drop(self, job: RowJob) -> None:
        with self._lock:
            self._file.save([j for j in self._file.load() if j.id != job.id])
