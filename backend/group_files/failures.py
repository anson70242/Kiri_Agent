"""What becomes of a group-file job after a failed attempt (a download or an upload).

Today every failure gives the job up: its local file is deleted and it is dropped. Ticket 07
replaces `after_failure` with the retries (the automatic download's interval and maximum of
errors) and the wait for a lapsed authorization.
"""

from __future__ import annotations

from .jobs import GroupFileJob


def after_failure(job: GroupFileJob, reason: str, lapsed: bool, now: float) -> GroupFileJob | None:
    """The job to keep and try again (changed as needed: its errors, its next try), or None
    when it is given up on. `lapsed`: the drive's authorization had lapsed."""
    return None
