"""The record of uploads: every group file the module uploaded, noted with its group, its
place on the drive, its size, its SHA-256 and the time, in the runtime state file
`group_file_uploads.json` next to the jobs file (a damaged one is set aside, the owner
alerted, and the service goes on empty).

It answers one question only: is the file at this place the same content as this one? What
is on the drive is decided by the drive's own listing, never by the record: a place noted
here whose file the owner deleted is simply not in the listing any more.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from backend.state_file import StateFile, expect_list

UPLOADS_FILE = "group_file_uploads.json"  # in the data directory, beside group_file_jobs.json


@dataclass(frozen=True)
class Upload:
    group: str  # the group's openid
    place: str  # `<群名>/<子文件夹>/<name on the drive>`, from the top of the drive
    size: int  # bytes
    sha256: str
    at: float  # when it was uploaded (epoch seconds)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Upload:
        return cls(**d)


class GroupFileUploads:
    """The record file. One entry a place: a place uploaded to again (its file was deleted
    on the drive and posted again) holds its latest upload."""

    def __init__(self, path: Path) -> None:
        self._file = StateFile(path, default=list, policy="recover",
                               decode=lambda raw: [Upload.from_dict(d) for d in expect_list(raw, "group-file uploads")],
                               encode=lambda uploads: [asdict(u) for u in uploads])

    @property
    def path(self) -> Path:
        return self._file.path

    def load(self) -> list[Upload]:
        return self._file.load()

    def note(self, upload: Upload) -> None:
        self._file.save([*(u for u in self.load() if u.place != upload.place), upload])

    def sha256_at(self, place: str) -> str | None:
        """The SHA-256 of the file uploaded to `place`; None when the record has none."""
        return next((u.sha256 for u in self.load() if u.place == place), None)
