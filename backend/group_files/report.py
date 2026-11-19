"""Where the outcome of a group-file job is told: one call per outcome, each made from one
place in the worker. Today the log is all; ticket 08 sends the group's messages and the
owner's alerts from here."""

from __future__ import annotations

import logging

from .jobs import GroupFileJob

log = logging.getLogger(__name__)


class GroupFileReport:
    async def uploaded(self, job: GroupFileJob, place: str) -> None:
        """The file is on the drive at `place` (`<群名>/<子文件夹>/<name on the drive>`)."""
        log.info("group file on the drive: %s", place)

    async def name_taken(self, job: GroupFileJob, place: str) -> None:
        """A file of that name is at `place` already: nothing was uploaded (ticket 06 makes
        this "already there", or uploads under another name)."""
        log.info("group file not uploaded, its name is taken on the drive: %s", place)

    async def failed(self, job: GroupFileJob, reason: str, given_up: bool) -> None:
        """An attempt failed with `reason`; `given_up`: the job is dropped, else it is tried
        again (see failures.after_failure)."""
        log.warning("group file %s/%s failed%s: %s", job.folder, job.name, ", given up" if given_up else "", reason)
