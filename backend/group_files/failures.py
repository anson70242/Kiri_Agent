"""What becomes of a group-file job after a failed attempt (a download or an upload), the way
the pipeline treats its drive jobs (backend.downloader.auto, `_finish_drive`):

- A failure is one error of the job. It is tried again after the automatic download's retry
  interval and given up on at its maximum of errors (`download.auto.retry_minutes` and
  `max_errors`: no settings of its own).
- A lapsed authorization of the drive is neither an attempt nor an error: the job is tried
  again at the same interval and given up on DRIVE_LAPSE_GIVE_UP (72 hours) after the first
  lapsed try of the lapse. Any try that is not lapsed ends the lapse.

`Failure` says which of these happened, so the report can tell the first error, the give-up
and the first lapsed try, and keep quiet about the rest.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum

from backend.downloader.auto import DRIVE_LAPSE_GIVE_UP

from .jobs import GroupFileJob


class Failure(StrEnum):
    FIRST_ERROR = "first_error"  # the job's first error: kept, tried again
    LATER_ERROR = "later_error"  # another error: kept, tried again
    GIVEN_UP = "given_up"  # too many errors, or the authorization stayed lapsed too long: dropped
    FIRST_LAPSE = "first_lapse"  # the first lapsed try of a lapse: kept, tried again
    LATER_LAPSE = "later_lapse"  # still lapsed: kept, tried again


@dataclass(frozen=True)
class AfterFailure:
    job: GroupFileJob  # the job as changed (errors, last error, next try, lapse); saved when kept
    failure: Failure
    reason: str  # what to tell: the attempt's reason, or why the lapse was given up on

    @property
    def kept(self) -> bool:
        return self.failure is not Failure.GIVEN_UP


def after_failure(job: GroupFileJob, reason: str, lapsed: bool, now: float, *,
                  retry_interval: float, max_errors: int) -> AfterFailure:
    """The job after an attempt that failed with `reason`; `lapsed`: the drive's
    authorization had lapsed. The attempt is over: its upload is no longer running."""
    job = dataclasses.replace(job, uploading=False, last_error=reason, next_try=now + retry_interval)
    if lapsed:
        first = job.lapsed_since is None
        if job.lapsed_since is None:
            job.lapsed_since = now
        if now - job.lapsed_since >= DRIVE_LAPSE_GIVE_UP:
            reason = f"网盘授权失效后等了 {DRIVE_LAPSE_GIVE_UP / 3600:g} 小时仍没有重新授权"
            job.last_error = reason
            return AfterFailure(job, Failure.GIVEN_UP, reason)
        return AfterFailure(job, Failure.FIRST_LAPSE if first else Failure.LATER_LAPSE, reason)
    job.lapsed_since = None  # a try that is not lapsed ends the lapse
    job.errors += 1
    if job.errors >= max_errors:
        return AfterFailure(job, Failure.GIVEN_UP, reason)
    return AfterFailure(job, Failure.FIRST_ERROR if job.errors == 1 else Failure.LATER_ERROR, reason)
