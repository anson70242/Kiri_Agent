"""The name a group file has on the drive, decided from the subfolder's listing just before
the upload.

The file keeps the name it was posted with (made safe as a file name) when that name is
free. When it is not, the record of uploads says whether the file there is the same
content (then nothing is uploaded), and otherwise the file goes beside it with the time
its message was posted: `【待校】第3话.2026-11-19-0541.ass`. Nothing on the drive is ever
replaced, deleted or moved.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .jobs import GroupFileJob
from .record import GroupFileUploads

# What a file name cannot hold on the server (Linux), plus control characters and the
# backslash, which only make trouble on the way.
UNSAFE = re.compile(r'[/\\\x00-\x1f]')

# The groups' local time (UTC+8): a name carries the time members saw in the chat.
GROUP_TIME = timezone(timedelta(hours=8))


def file_name(name: str) -> str:
    """The posted name as a file name on the server: unsafe characters become `_`."""
    safe = UNSAFE.sub("_", name).strip()
    return "_" if safe in ("", ".", "..") else safe


@dataclass(frozen=True)
class DriveName:
    name: str  # the file's name in its subfolder
    upload: bool  # False: the same file is there under `name`, the job ends as "already there"


def drive_name(job: GroupFileJob, names: list[str], uploads: GroupFileUploads) -> DriveName:
    """The decision for `job` given the `names` in its subfolder on the drive and the record
    of uploads: its own name when free; nothing to upload when the file of that name, or of
    that name with a time, is the same content; else its name with the time it was posted
    (the seconds added when that is taken too, then a number)."""
    name = file_name(job.name)
    if name not in names:
        return DriveName(name, upload=True)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    # The name itself first, then the names this module gave earlier versions (the time, with
    # the seconds and a number when taken): a version posted again is not uploaded again.
    # Only names in the listing: the drive decides what is there.
    dated = re.compile(rf"{re.escape(stem)}\.\d{{4}}-\d{{2}}-\d{{2}}-\d{{4}}(?:\d{{2}}(?:-\d+)?)?{re.escape(dot + ext)}")
    versions = [name, *(n for n in names if dated.fullmatch(n))]
    for there in versions:
        if uploads.sha256_at(f"{job.folder}/{there}") == job.sha256:
            return DriveName(there, upload=False)
    posted = datetime.fromtimestamp(job.posted_at, GROUP_TIME)
    minute, second = posted.strftime("%Y-%m-%d-%H%M"), posted.strftime("%Y-%m-%d-%H%M%S")
    stamps = itertools.chain([minute, second], (f"{second}-{n}" for n in itertools.count(2)))
    return next(DriveName(dated, upload=True) for dated in (f"{stem}.{s}{dot}{ext}" for s in stamps)
                if dated not in names)