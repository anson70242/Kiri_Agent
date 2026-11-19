"""The name a group file has on the drive, decided from the subfolder's listing just before
the upload.

Today: the name it was posted with (made safe as a file name), and when that name is in the
folder already nothing is uploaded. Ticket 06 replaces `drive_name` with the full decision:
the same content already there, or a name with the time it was posted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .jobs import GroupFileJob

# What a file name cannot hold on the server (Linux), plus control characters and the
# backslash, which only make trouble on the way.
UNSAFE = re.compile(r'[/\\\x00-\x1f]')


def file_name(name: str) -> str:
    """The posted name as a file name on the server: unsafe characters become `_`."""
    safe = UNSAFE.sub("_", name).strip()
    return "_" if safe in ("", ".", "..") else safe


@dataclass(frozen=True)
class DriveName:
    name: str  # the file's name in its subfolder
    upload: bool  # False: nothing is uploaded, the job ends (the name is taken)


def drive_name(job: GroupFileJob, names: list[str]) -> DriveName:
    """The decision for `job` given the `names` in its subfolder on the drive."""
    name = file_name(job.name)
    return DriveName(name, upload=name not in names)
