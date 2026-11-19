"""Group files to the drive: a file posted in a QQ group whose name a filing rule keeps is
downloaded from QQ at once and uploaded into `<群名>/<subfolder>` on the cloud drive. See
docs/adr/0002-group-files-through-the-official-bots-message-events.md.

    gateway --posted()--> GroupFileKeeper --run()--> download, then upload, one at a time

One concern a file:

- rules.py     the filing rules: the table, the matching, the help's lines
- jobs.py      a job, the jobs file (runtime state), which job goes next
- download.py  the download from QQ's file host (https, qq.com only; size; SHA-256)
- naming.py    the name on the drive, decided from the subfolder's listing
- record.py    the record of uploads (runtime state): is the file there the same content?
- failures.py  what becomes of a job after a failed attempt
- report.py    where each outcome is told (the log; messages later)
- worker.py    GroupFileKeeper: takes files in, runs the loops, the snapshot
"""

from .jobs import JOBS_FILE, GroupFileJob, GroupFileJobs
from .worker import GroupFileKeeper

__all__ = ["JOBS_FILE", "GroupFileJob", "GroupFileJobs", "GroupFileKeeper"]
