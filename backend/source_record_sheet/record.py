"""The record of rows written: which row of which live went into which tab, in the runtime
state file `source_record_rows.json` beside the jobs file (a damaged one is set aside, the
owner alerted, and the service goes on empty). It holds no link.

It answers one question: was this row written into this tab already? A second telling of
the same live (a restart between the message and the save, a second message) then makes
no second row. It keeps the newest RECORD_CAP rows; the oldest go first.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from backend.state_file import StateFile, expect_list

from .row import RowKind

ROWS_FILE = "source_record_rows.json"  # in the data directory, beside source_record_jobs.json
# Rows remembered. A live is told once or twice, within hours or days of being processed;
# two rows a live for a few streamers and groups keep a year or so here.
RECORD_CAP = 2000


@dataclass(frozen=True)
class RowKey:
    """One row of one live in one tab. A live is its streamer (roster name) and stream id."""

    file_id: str
    tab_id: str
    streamer: str
    stream_id: str
    kind: RowKind

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RowKey:
        return cls(**{**d, "kind": RowKind(d["kind"])})


class RowRecord:
    """The record file: the rows written, oldest first."""

    def __init__(self, path: Path, cap: int = RECORD_CAP) -> None:
        self._file = StateFile(path, default=list, policy="recover",
                               decode=lambda raw: [RowKey.from_dict(d) for d in expect_list(raw, "source record rows")],
                               encode=lambda keys: [asdict(k) for k in keys])
        self.cap = cap
        self._lock = threading.Lock()

    def load(self) -> list[RowKey]:
        with self._lock:
            return self._file.load()

    def written(self, key: RowKey) -> bool:
        return key in self.load()

    def note(self, key: RowKey) -> None:
        with self._lock:
            keys = [k for k in self._file.load() if k != key]
            self._file.save([*keys, key][-self.cap:])
