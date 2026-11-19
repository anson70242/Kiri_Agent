"""Where the outcome of a group-file job is told: one call per outcome, each made from one
place in the worker.

The group the file came from is told, when its notice switch is on (and it is not marked as
removed): a reply to the member's message while QQ still takes one, else an ordinary
message. Every failure also goes to the owner as an operator alert (Discord), whatever the
switch says; a lapsed authorization once per lapse, with the command to run. Successes do
not. Every message sent or failed is noted in the notification history. These are not
pipeline events: no streamer, no 通知类型, no cover.

Telling never raises: a message that could not be delivered does not touch the job.
Without the QQ API and the alert (the default) the log is all.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from backend.drive import LOGIN_COMMAND
from backend.notify.history import NotifyHistory
from backend.notify.qq import QQApi, QQError, QQGroups, escape, send_with_tries
from backend.quark import RunningUpload, UploadKind

from .failures import Failure
from .jobs import GroupFileJob

log = logging.getLogger(__name__)

# QQ takes a reply to a member's message for 5 minutes after it (its documented rule); a
# notice later than this is an ordinary message. The margin covers the time the message
# takes to reach QQ, and a server clock a little off from QQ's.
REPLY_WITHIN = 4.5 * 60

FOOTER = "切片小助手 群文件"
# Headlines: (icon, words). The group's message shows both, the owner's alert the words.
ON_DRIVE = ("☁️", "群文件已上传网盘")
ALREADY_THERE = ("☁️", "群文件已在网盘，没有重复上传")
FAILED = ("❗", "群文件上传网盘失败")
GIVEN_UP = ("❗", "群文件上传网盘失败，已放弃")
WAITING = ("⏳", "群文件排队中")
RETRIED_LATER = "稍后自动重试"
GOES_UP_BY_ITSELF = "轮到后自动上传"
LAPSED = "网盘授权已失效，重新授权后会自动上传"
LAPSE_ALERT = f"夸克网盘的授权失效了，群文件的网盘上传都在等待。在服务器上重新授权：\n`{LOGIN_COMMAND}`"
REASON_KEPT = 500  # characters of a failure's reason that are told (Quark's words can be long)


def place_line(place: str) -> str:
    return f"网盘位置：{place}"


def busy_line(running: RunningUpload) -> str:
    """What the drive is busy with: the kind of upload only, never a recording's title or
    another file's name; how far along when the CLI has said."""
    what = "其他群文件" if running.kind is UploadKind.GROUP_FILE else "一场录播"
    percent = running.percent
    return f"网盘正在上传{what}" + ("" if percent is None else f"（{percent}%）")


class GroupFileReport:
    def __init__(self, api: QQApi | None = None, groups: QQGroups | None = None, *,
                 alert: Callable[[str], Any] | None = None, history: NotifyHistory | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.api, self.groups = api, groups  # None: no group is told
        self.alert = alert  # the operator alert (LiveNotifier.alert: Discord); None: none
        self.history = history
        self.clock = clock  # the keeper's: a job's `posted_at` is on it
        # The owner was alerted that the drive's authorization lapsed. An upload try that is
        # not lapsed ends the lapse: a later one is alerted again. Not saved: after a
        # restart a lapse that goes on is alerted once more (accepted).
        self._lapse_alerted = False

    # ---- the outcomes (the worker calls these) ----

    async def uploaded(self, job: GroupFileJob, place: str) -> None:
        """The file is on the drive at `place` (`<群名>/<子文件夹>/<name on the drive>`)."""
        log.info("group file on the drive: %s", place)
        self._lapse_alerted = False
        await self.tell(job, ON_DRIVE, place_line(place))

    async def already_there(self, job: GroupFileJob, place: str) -> None:
        """The same file (by the record of uploads) is at `place` already: nothing was uploaded."""
        log.info("group file already on the drive, not uploaded again: %s", place)
        self._lapse_alerted = False
        await self.tell(job, ALREADY_THERE, place_line(place))

    async def failed(self, job: GroupFileJob, reason: str, failure: Failure) -> None:
        """An attempt failed with `reason`. `failure` says what became of the job (see
        failures.py): its first error, a later one, given up on (dropped), or the first or a
        later lapsed try of a lapse of the drive's authorization. `job` is as saved after
        the attempt (its errors, its next try, its lapse)."""
        log.warning("group file %s/%s failed (%s, %d error(s)): %s", job.folder, job.name, failure, job.errors, reason)
        if job.downloaded and job.lapsed_since is None:
            self._lapse_alerted = False  # the drive answered an upload without a lapse
        reason = reason[:REASON_KEPT]
        if failure is Failure.FIRST_ERROR:
            self._alert(job, FAILED, reason, RETRIED_LATER)
            await self.tell(job, FAILED, reason, RETRIED_LATER)
        elif failure is Failure.GIVEN_UP:
            self._alert(job, GIVEN_UP, reason)
            await self.tell(job, GIVEN_UP, reason)
        elif failure is Failure.FIRST_LAPSE:
            if not self._lapse_alerted:  # once per lapse, however many files wait
                self._lapse_alerted = True
                self._alert(job, FAILED, LAPSE_ALERT)
            await self.tell(job, FAILED, LAPSED)
        # A later error or lapsed try is silent: the group was told at the first.

    async def waiting(self, job: GroupFileJob, running: RunningUpload) -> None:
        """The file has waited a while for the upload that has the drive (`running`) to end.
        Not an error: no alert."""
        busy = busy_line(running)
        log.info("group file %s/%s waits for the drive: %s", job.folder, job.name, busy)
        await self.tell(job, WAITING, busy, GOES_UP_BY_ITSELF)

    # ---- telling ----

    async def tell(self, job: GroupFileJob, headline: tuple[str, str], *lines: str) -> None:
        """Tell the group the file came from, when its notice switch is on: the headline, the
        file's name as posted, `lines`, the footer. A reply to the member's message while QQ
        takes one; later, or when QQ refuses the reply, an ordinary message; each with the
        usual tries of a QQ message. Noted in the history, sent or not."""
        if self.api is None or self.groups is None:
            return
        try:
            files = self.groups.group_files(job.group)
            if files is None or not files.notices:
                return
            text = "\n".join([" ".join(headline), escape(job.name), *(escape(line) for line in lines), FOOTER])
            summary = f"{' '.join(headline)}：{job.name}"
            try:
                await self._send(job, text)
            except (QQError, httpx.HTTPError) as e:
                error = str(e) or type(e).__name__
                log.warning("qq: %s not delivered to %s: %s", summary, job.group_folder, error)
                self._note(job, summary, False, error)
                return
            self._note(job, summary, True)
        except Exception:  # noqa: BLE001 - telling never touches the job
            log.exception("group file %s/%s: the group was not told", job.folder, job.name)

    async def _send(self, job: GroupFileJob, text: str) -> None:
        """With the usual tries of a QQ message (`send_with_tries`)."""
        assert self.api
        if job.msg_id and self.clock() - job.posted_at < REPLY_WITHIN:
            try:
                await send_with_tries(self.api, job.group, text, msg_id=job.msg_id)
                return
            except QQError as e:  # refused as a reply: the same text as an ordinary message
                log.info("qq: a group-file notice was refused as a reply (%s); sending it as a message", e)
        await send_with_tries(self.api, job.group, text)

    def _note(self, job: GroupFileJob, summary: str, ok: bool, error: str = "") -> None:
        if self.history:
            self.history.add("qq", job.group, summary, ok, error)

    def _alert(self, job: GroupFileJob, headline: tuple[str, str], *lines: str) -> None:
        """The owner's alert (plain text): the group's name with the headline's words, the
        file's name, `lines`. Whatever the group's notice switch says."""
        if self.alert is None:
            return
        try:
            self.alert("\n".join([f"QQ 群「{job.group_folder}」{headline[1]}", job.name, *lines]))
        except Exception:  # noqa: BLE001 - an alert never touches the job
            log.exception("group file %s/%s: the owner was not alerted", job.folder, job.name)
