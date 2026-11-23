"""Download a stream's archive automatically once it ends.

    LiveNotifier ("stream ended") -> AutoDownloader.on_ended (queue a job)
    -> AutoDownloader.run (a download worker and a transcription worker, each one job
       at a time, in threads) -> Discord via LiveNotifier.send

Streamers (the roster, backend/monitor/roster.py; set from the dashboard's gear):

    { "name": "...", "platform": "youtube", "channel": "@...", "auto_download": true, "keep": 6 }

config.json:

    "download": { "auto": {
      "keep": 6,              # archives kept per streamer folder; the oldest go first
      "retry_minutes": 15,
      "give_up_hours": 12,    # stop waiting for a VOD that isn't ready after this long
      "max_errors": 3,        # other failures (cookies, deleted video...) before giving up
      "state_file": "data/download_queue.json"
    }},
    "drive": { "upload_hours": [3, 9] }   # a drive copy's upload starts only from 03:00 to
                                          # 09:00, Hong Kong time; without it, at any time

Rules:
- Not ready yet (YouTube still processing, Twitch replay not listed): retried every
  `retry_minutes` until `give_up_hours` after the live ended, then a failure message.
- Replay not published (TwitCasting): a live shorter than SHORT_LIVE is dropped at once,
  without a message. A longer one (or one whose length is not known) gets one notice,
  then is retried like "not ready yet" and dropped at `give_up_hours` without a message.
- Any other failure: retried until `max_errors`, then a failure message on Discord.
- Success: a Discord message, then the streamer folder is trimmed to `keep` archives; an
  archive that still has a job in the queue, at any stage, is spared until a later prune.
- Every stage over: "全部处理完成" (READY), with what the live has and the site's links; the
  source record sheet is told the 全部下载 link it carries (`source_record`). For
  a streamer whose gear ticks 自动上传到夸克网盘, when the drive is available and the archive
  has a video, a drive job is queued in the same save. While the live's chat is still in
  the chat retry queue, both READY and the upload wait; then READY goes out, the drive copy
  is uploaded, and 网盘上传完成 brings the drive link, which the source record sheet is told
  too (backend/drive.py is the drive's side):
  READY never waits for the upload. When its first try does not upload, 网盘上传失败 says
  so; it is retried like any stage and only its give-up is told again. A lapsed
  authorization is neither an attempt nor an error: 网盘授权失效 once per lapse, and each
  job waits up to DRIVE_LAPSE_GIVE_UP for the owner to authorize again. With drive upload
  hours, an upload starts only inside them (a retry too): outside, the job waits for their
  next beginning, which is neither an attempt nor an error and is not told; an upload that
  is running when they end goes on to its end. READY and group files are not held by them.
- Flash transfers (docs/adr/0003-flash-transfers-through-an-outside-qq-client-service.md):
  for a streamer whose gear ticks 自动上传到 QQ 闪传, when a flash service is configured
  (`flash`, backend/flash/client.py) and the archive has a video, a flash job is queued in
  the same save as the drive job (both may be; neither waits for the other). Its live's
  READY is then the flash job's to send, and nobody else's: it waits for a late chat, then
  the flash job prepares the files (backend/flash/files.py), starts the flash transfer and
  saves it in the job, and looks at its progress every FLASH_PROGRESS_LOOK seconds until
  it is uploaded. Then the flash link is noted in the archive and READY goes out with it
  (no short download link is made), and the source record sheet is told the flash link. A
  failed try (an error, a failed file, an upload that has not moved for FLASH_STALL, the
  files that could not be prepared) is retried like any stage, each try a new flash
  transfer; at `max_errors` it is given up on: READY goes out as without one, and the owner
  gets one operator alert (`alert`). One flash transfer at a time, in the order the lives
  were processed; a failed one waits for its next try without holding the others. While
  the flash account is offline or the flash service cannot be reached, a flash job waits
  instead: neither an attempt nor an error, nothing prepared before the account is online,
  looked at every FLASH_OFFLINE_LOOK; the owner is reminded at once and then at most every
  FLASH_REMINDER_EVERY while a reminded live is in the queue (operator alert, one for all
  waiting lives). The waiting ends when the flash service serves the stage again (a flash
  transfer started, a started one's progress answered), not when its status merely says
  online: then the files are prepared at most once a retry pause, and the lives go on in
  their order once it serves. A live that has waited FLASH_OFFLINE_GIVE_UP since its first
  wait gets READY as without a flash transfer, without the alert of a failure.
- Jobs live in `state_file`, so a restart resumes them (yt-dlp resumes partial files).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from backend.analysis import AnalysisSettings, analyze, is_singing_live, song_naming
from backend.archive import Archive, Artifact, ArtifactDamaged, ArtifactMissing, Downloads, file_date, read_file
from backend.asr import ASRSettings, transcribe
from backend.drive import LOGIN_COMMAND as DRIVE_LOGIN_COMMAND
from backend.drive import Drive, DriveAuthorizationLapsed, DriveCopy, DriveFailed
from backend.drive_copy import remove_leftover_drive_copies
from backend.flash.client import STALL_SECONDS, FlashClient, FlashFailed, FlashUnavailable, StartedTransfer
from backend.flash.files import needs_cutting, note_flash_link, prepare, remove_leftover_staging, remove_staging
from backend.llm import LLMError, chat, load_llm
from backend.tencent_docs import (
    TencentDocsError, load_credentials, sheet_title, upload_table)
from backend.translate import TranslateSettings
from backend.translate import translate as translate_transcript
from backend.monitor import StreamMonitor, Streamer, StreamStatus
from backend.monitor.roster import load_roster
from backend.notify import EventKind, Link, LiveNotifier, StreamEvent, load_notifier
from backend.state_file import StateFile, expect_list

from . import REPO_ROOT, download, downloader_for, load_settings
from .base import DownloadError, DownloadResult, DownloadSettings, ReplayUnpublished
from .pending import PendingChats

if TYPE_CHECKING:
    from backend.source_record_sheet import SourceRecordKeeper

log = logging.getLogger(__name__)

NOT_READY = ("is_live", "is_upcoming", "post_live")
STAGES = ("download", "transcribe", "analyze", "translate", "upload", "drive", "flash")
AUTH_HINTS = ("member", "sign in", "cookie", "login", "subscriber", "private")
# A live shorter than this whose replay is not published is dropped without a word: test
# lives and the fragments a dropped connection leaves.
SHORT_LIVE = 5 * 60
# The defaults of download.auto.retry_minutes (in seconds) and max_errors; the group-files
# module's retries take the same.
RETRY_INTERVAL = 15 * 60
MAX_ERRORS = 3


@dataclass
class Job:
    status: StreamStatus  # the stream's last live status (url, title, start time)
    queued_at: float
    next_try: float
    attempts: int = 0
    errors: int = 0  # failures other than "not ready yet"
    last_error: str = ""
    stage: Literal["download", "transcribe", "analyze", "translate", "upload", "drive", "flash"] = "download"
    video: str | None = None  # set once downloaded
    live_ended_at: float | None = None  # when the live ended (epoch seconds); None: not known
    unpublished_notice_sent: bool = False  # the owner was told the replay is not published
    # Drive stage: "全部处理完成" is not this job's to send: it went out for this live, or the
    # live's flash job sends it.
    ready_sent: bool = False
    lapsed_since: float | None = None  # drive stage: its first try of the current lapse
    uploading: bool = False  # drive stage: its upload is running (a deploy waits for it)
    # Flash stage: its video is being cut for the flash transfer (a deploy waits for it: the
    # cutting would start over). Only the cutting marks a flash job; its upload runs in the
    # flash service and goes on by its id after a restart.
    cutting: bool = False
    # The address of the replay's own cover, from its metadata ("" when it has none); None
    # until the replay is downloaded. The messages show the picture `StreamStatus.as_ended`
    # picks with it.
    replay_cover: str | None = None
    # Flash stage: the flash transfer started (its id, flash link, the link's expiry and when
    # it was started, epoch seconds); None until it is. The link is never logged.
    transfer_id: str | None = None
    flash_link: str | None = field(default=None, repr=False)
    flash_expires: float | None = None
    flash_made: float | None = None
    # Flash stage, a hung upload: the bytes sent at the last look at the started flash
    # transfer's progress and when they last moved (epoch seconds); None until one is started.
    flash_sent: int | None = None
    flash_moved_at: float | None = None
    # Flash stage, the flash account offline (or the flash service unreachable): when this job
    # first found it so (its FLASH_OFFLINE_GIVE_UP count from then; None while it does not
    # wait), and when the owner was last reminded of a waiting that covered it (kept while the
    # job is in the queue, so that no second reminder comes within FLASH_REMINDER_EVERY).
    # A waiting ends only when the flash service serves the stage again (a flash transfer
    # started, a started one's progress answered), not when its status merely says online:
    # `flash_status_online` notes that the waiting job's last look found it so, and its next
    # try prepares the files and starts the flash transfer.
    flash_offline_since: float | None = None
    flash_reminded_at: float | None = None
    flash_status_online: bool = False

    @property
    def stream_key(self) -> str:
        """One broadcast (a stream can have an analysis and a translation job at once)."""
        return f"{self.status.streamer.id}|{self.status.stream_id or self.status.url}"

    @property
    def key(self) -> str:
        return f"{self.stream_key}|{self.stage}"

    @property
    def short_live(self) -> bool:
        """The live lasted less than SHORT_LIVE. Without a start or end time it counts as long."""
        started = self.status.started_at
        if started is None or self.live_ended_at is None:
            return False
        return self.live_ended_at - started.timestamp() < SHORT_LIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.to_dict(),
            "queued_at": self.queued_at,
            "next_try": self.next_try,
            "attempts": self.attempts,
            "errors": self.errors,
            "last_error": self.last_error,
            "stage": self.stage,
            "video": self.video,
            "live_ended_at": self.live_ended_at,
            "unpublished_notice_sent": self.unpublished_notice_sent,
            "ready_sent": self.ready_sent,
            "lapsed_since": self.lapsed_since,
            "uploading": self.uploading,
            "cutting": self.cutting,
            "replay_cover": self.replay_cover,
            "transfer_id": self.transfer_id,
            "flash_link": self.flash_link,
            "flash_expires": self.flash_expires,
            "flash_made": self.flash_made,
            "flash_sent": self.flash_sent,
            "flash_moved_at": self.flash_moved_at,
            "flash_offline_since": self.flash_offline_since,
            "flash_reminded_at": self.flash_reminded_at,
            "flash_status_online": self.flash_status_online,
        }

    @property
    def transfer(self) -> StartedTransfer | None:
        """Flash stage: the flash transfer started for this live, None until it is."""
        if self.transfer_id is None or not self.flash_link or self.flash_expires is None or self.flash_made is None:
            return None
        return StartedTransfer(self.transfer_id, self.flash_link,
                               datetime.fromtimestamp(self.flash_expires, timezone.utc),
                               datetime.fromtimestamp(self.flash_made, timezone.utc))

    @transfer.setter
    def transfer(self, started: StartedTransfer | None) -> None:
        self.transfer_id = started.id if started else None
        self.flash_link = started.link if started else None
        self.flash_expires = started.expires.timestamp() if started else None
        self.flash_made = started.made.timestamp() if started else None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        return cls(**d | {"status": StreamStatus.from_dict(d["status"])})


def decode_jobs(raw: Any) -> list[Job]:
    """The download queue file's JSON -> jobs; raises if it isn't a list of jobs."""
    return [Job.from_dict(d) for d in expect_list(raw, "jobs")]


@dataclass
class Outcome:
    # lapsed: the drive's authorization; unavailable: no drive (flash service) any more for a
    # drive (flash) job; offline: a flash job waits for the flash account; waited_out: it
    # waited FLASH_OFFLINE_GIVE_UP and is given up on
    kind: Literal["done", "wait", "unpublished", "error", "lapsed", "unavailable", "offline", "waited_out"]
    message: str = ""
    retry_at: float | None = None  # drive and flash stages, wait: not to be tried again before this time
    result: DownloadResult | None = None
    pruned: list[Path] = field(default_factory=list)
    prune_error: str = ""  # done, but deleting old archives raised this (type: message)
    drive: DriveCopy | None = None  # drive stage, done: what the drive answered
    # Flash stage: the flash transfer just started (wait: to be saved in the job), or the one
    # uploaded (done)
    flash: StartedTransfer | None = None
    sent: int | None = None  # flash stage, wait while uploading: the bytes sent at this look
    # Flash stage: the flash service served this look (a flash transfer started, its progress
    # answered): no live waits for the flash account any more
    served: bool = False
    online: bool = False  # flash stage: a waiting job's look found the status online, nothing served


class AutoDownloader:
    def __init__(
        self,
        settings: DownloadSettings,
        streamers: Iterable[Streamer],
        keep: dict[str, int],  # streamer id -> archives to keep; only these auto-download
        state_file: Path,
        retry_interval: float = RETRY_INTERVAL,
        give_up: float = 12 * 3600,
        max_errors: int = MAX_ERRORS,
        default_keep: int = 6,  # for streamers without their own `keep`
        notify: Callable[[StreamEvent], None] | None = None,
        clock: Callable[[], float] = time.time,
        # video -> transcript, called with singing=True for a singing live; None: skip
        transcribe: Callable[..., Any] | None = None,
        analyze: Callable[[Path, str, str], Path] | None = None,  # (video, streamer, title) -> table
        translate: Callable[[Path], Any] | None = None,  # video -> Chinese subtitles; None: skip
        # (路灯表, spreadsheet id, streamer, title) -> sheet link; None: no credentials, skip
        upload: Callable[[Path, str, str, str], str] | None = None,
        drive: Drive | None = None,  # uploads drive copies; None: the drive is not available
        # (from, until): the hours of Hong Kong time between which a drive copy's upload may
        # start; None: at any time
        drive_hours: tuple[int, int] | None = None,
        flash: FlashClient | None = None,  # makes flash transfers; None: no flash service is configured
    ):
        self.upload = upload
        self.drive = drive
        self.drive_hours = drive_hours
        self.flash = flash
        self.transcribe = transcribe
        self.analyze = analyze
        self.translate = translate
        self.settings = settings
        self.streamers = list(streamers)
        self.keep = keep
        # Runtime state: a damaged queue is set aside as a backup (the owner is alerted).
        self._queue = StateFile(state_file, default=list, policy="recover", decode=decode_jobs,
                                encode=lambda jobs: [j.to_dict() for j in jobs])
        self.retry_interval = retry_interval
        self.give_up = give_up
        self.max_errors = max_errors
        self.default_keep = default_keep
        self.uploads: dict[str, str] = {}  # streamer id -> Tencent Docs spreadsheet id (the gear)
        self.drive_folders: dict[str, str] = {}  # streamer id -> folder on the Quark drive (the gear)
        self.flash_streamers: set[str] = set()  # streamer ids whose lives get a flash transfer (the gear)
        self.site_url = ""  # the dashboard's address (SITE_URL in .env): links in messages
        # A new short download link for the archive at a folder reference, so that the 全部下载
        # address downloads without logging in; None when the dashboard has no login.
        self.download_link: Callable[[str], ShortDownloadLink] | None = None
        # Told when "全部处理完成" goes out with a 全部下载 link; None: nobody records lives.
        self.source_record: SourceRecordKeeper | None = None
        # The owner was told the drive's authorization lapsed; any try that is not lapsed (a
        # failure as well as a success) ends the lapse. Not saved: after a restart a lapse is
        # told once more.
        self._lapse_alerted = False
        self.notify = notify
        # The owner's operator alert (Discord only; `LiveNotifier.alert`, called on the event
        # loop's thread): a flash transfer given up on. None: nobody is told.
        self.alert: Callable[[str], None] | None = None
        self.clock = clock
        # One worker per stage, so a long transcription never holds up downloads.
        self._wake = {stage: asyncio.Event() for stage in STAGES}
        self._active: dict[str, dict[str, Any] | None] = dict.fromkeys(STAGES)  # job + progress

    # ---- queue ----

    def load(self) -> list[Job]:
        return self._queue.load()

    def save(self, jobs: Iterable[Job]) -> None:
        self._queue.save(list(jobs))

    def on_ended(self, event: StreamEvent) -> None:
        """LiveNotifier.on_ended listener: queue the archive of a stream that ended."""
        status = event.status
        if status.streamer.id not in self.keep or not status.url:
            return
        jobs = self.load()
        now = self.clock()
        job = Job(status, queued_at=now, next_try=now,
                  live_ended_at=event.ended_at.timestamp() if event.ended_at else None)
        if any(j.stream_key == job.stream_key for j in jobs):
            return
        jobs.append(job)
        self.save(jobs)
        log.info("auto-download queued: %s %s", status.streamer.name, status.url)
        self._wake["download"].set()

    def snapshot(self) -> dict[str, Any]:
        """What's downloading now and what's queued, for the archives page."""
        return {
            # copies: the worker threads mutate them
            "active": [dict(a) for stage in STAGES if (a := self._active[stage])],
            "queue": [
                {
                    "name": j.status.streamer.name,
                    "platform": j.status.streamer.platform,
                    "title": j.status.title,
                    "url": j.status.url,
                    "queued_at": j.queued_at,
                    "next_try": j.next_try,
                    "attempts": j.attempts,
                    "last_error": j.last_error,
                    "stage": j.stage,
                }
                for j in self.load()
            ],
        }

    # ---- worker ----

    async def run(self) -> None:
        """Process due jobs forever: one download and one transcription at a time,
        side by side. The download worker also retries late chats."""
        jobs = self.load()
        if any(j.uploading or j.cutting for j in jobs):  # left by a kill: that upload or cutting is gone, and done again
            for j in jobs:
                j.uploading = j.cutting = False
            self.save(jobs)
        remove_leftover_drive_copies(self.settings.output_dir)
        # A started flash transfer goes on in the flash service, reading its staging folder;
        # any other staging folder was left by a kill (a flash job without an id prepares again).
        remove_leftover_staging(self.settings.output_dir,
                                keep=[Path(j.video).parent for j in jobs if j.stage == "flash" and j.transfer_id and j.video])
        await asyncio.gather(*(self.run_stage(stage) for stage in STAGES))

    async def run_stage(self, stage: str) -> None:
        wake = self._wake[stage]
        while True:
            wake.clear()
            try:
                await self.run_due(stage)
                if stage == "download":
                    await self.retry_chats()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("%s worker error", stage)
            try:
                await asyncio.wait_for(wake.wait(), self.sleep_time(stage))
            except TimeoutError:
                pass

    def sleep_time(self, stage: str | None = None) -> float:
        times = [j.next_try for j in self._eligible(self.load(), stage)]
        if stage in (None, "download") and (queue := PendingChats.from_settings(self.settings)):
            if (due := queue.next_due()) is not None:
                times.append(due)
        if not times:
            return 300
        return min(300.0, max(1.0, min(times) - self.clock()))

    async def run_due(self, stage: str | None = None) -> None:
        while job := self.next_due(stage):
            # Marked here, not in the thread: one writer of the queue at a time.
            if job.stage == "drive":
                self._mark_busy(job, "uploading")
            elif job.stage == "flash" and self._cuts(job):
                self._mark_busy(job, "cutting")
            outcome = await asyncio.to_thread(self.attempt, job)
            self.finish(job, outcome)

    def _mark_busy(self, job: Job, mark: Literal["uploading", "cutting"]) -> None:
        """Note in the queue file that the job's work a deploy waits for is running: a drive
        job's upload, a flash job's cutting (the attempt's end clears it)."""
        jobs = self.load()
        for j in jobs:
            if j.key == job.key:
                setattr(j, mark, True)
        self.save(jobs)

    def _cuts(self, job: Job) -> bool:
        """This try at the flash job prepares its files and cuts its video: no flash transfer
        is started yet, the late chat is not awaited, the try does more than look at the
        flash account, and the video is over the limit."""
        if job.transfer_id or not self.flash or not job.video or self._awaits_chat(job):
            return False
        if self._looks_only(job) or self._goes_after(job, self.load()):
            return False
        return needs_cutting(Archive.from_video(Path(job.video)))

    def next_due(self, stage: str | None = None) -> Job | None:
        now = self.clock()
        due = [j for j in self._eligible(self.load(), stage) if j.next_try <= now]
        # Flash transfers go in the order the lives were processed, the other stages' jobs
        # in the order they came due.
        return min(due, key=lambda j: j.queued_at if j.stage == "flash" else j.next_try) if due else None

    @staticmethod
    def _eligible(jobs: list[Job], stage: str | None) -> list[Job]:
        """The stage's jobs that may be tried when due: while a flash transfer is started, no
        other flash job (one flash transfer at a time), unless it waits for the flash account:
        then the others are looked at too, and wait with it (`_goes_after`)."""
        jobs = [j for j in jobs if stage in (None, j.stage)]
        started = [j for j in jobs if j.stage == "flash" and j.transfer_id]
        if started and any(j.flash_offline_since is None for j in started):
            jobs = [j for j in jobs if j.stage != "flash" or j.transfer_id]
        return jobs

    def attempt(self, job: Job) -> Outcome:
        """One try at the job's current stage (blocking)."""
        if job.stage == "transcribe":
            return self.attempt_transcribe(job)
        if job.stage == "analyze":
            return self.attempt_analyze(job)
        if job.stage == "translate":
            return self.attempt_translate(job)
        if job.stage == "upload":
            return self.attempt_upload(job)
        if job.stage == "drive":
            return self.attempt_drive(job)
        if job.stage == "flash":
            return self.attempt_flash(job)
        s = job.status
        try:
            dl = downloader_for(s.url or "", self.settings, self.streamers)
            url = dl.find_replay(s.url or "", s.streamer.channel, s.stream_id, s.started_at)
            if not url:
                return Outcome("wait", "archive not listed yet")
            info = dl.info(url)
            if info.live_status in NOT_READY:
                return Outcome("wait", f"stream is {info.live_status}")
            self._active["download"] = {
                "name": s.streamer.name, "platform": s.streamer.platform, "title": s.title, "url": s.url,
                "started": self.clock(), "stage": "download",
                "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
            result = download(url, self.settings, self.streamers, info=info, progress=self._on_progress)
            if err := result.errors.get("video"):
                return Outcome("error", err, result)
        except ReplayUnpublished as e:
            return Outcome("unpublished", str(e))
        except DownloadError as e:
            return Outcome("error", str(e))
        except Exception as e:  # noqa: BLE001 - a bug or a surprise: one failed attempt, not a stuck job
            log.exception("auto-download attempt raised: %s %s", s.streamer.name, s.url)
            return Outcome("error", f"{type(e).__name__}: {e}")
        finally:
            self._active["download"] = None
        try:
            pruned = self.prune(result.output_dir, self.keep.get(s.streamer.id, 6))
        except Exception as e:  # noqa: BLE001 - the video is there: a failed cleanup must not fail the download
            log.exception("pruning old archives failed after downloading: %s %s", s.streamer.name, s.url)
            return Outcome("done", result=result, prune_error=f"{type(e).__name__}: {e}")
        return Outcome("done", result=result, pruned=pruned)

    def attempt_transcribe(self, job: Job) -> Outcome:
        s = job.status
        self._active["transcribe"] = {
            "name": s.streamer.name, "platform": s.streamer.platform, "title": s.title, "url": s.url,
            "started": self.clock(), "stage": "transcribe",
            "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
        try:
            assert self.transcribe and job.video
            # A singing live is transcribed whole, so its lyrics are there for song naming.
            extra = {"singing": True} if is_singing_live(s.title or "") else {}
            self.transcribe(Path(job.video), **extra)
        except Exception as e:  # noqa: BLE001 - ASR, ffmpeg or a missing file: all retried
            return Outcome("error", str(e) or type(e).__name__)
        finally:
            self._active["transcribe"] = None
        return Outcome("done")

    def attempt_analyze(self, job: Job) -> Outcome:
        s = job.status
        self._active["analyze"] = {
            "name": s.streamer.name, "platform": s.streamer.platform, "title": s.title, "url": s.url,
            "started": self.clock(), "stage": "analyze",
            "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
        try:
            assert self.analyze and job.video
            out = self.analyze(Path(job.video), s.streamer.name, s.title or "")
        except Exception as e:  # noqa: BLE001 - LLM down, bad JSON, missing transcript: all retried
            return Outcome("error", str(e) or type(e).__name__)
        finally:
            self._active["analyze"] = None
        return Outcome("done", str(out))

    def attempt_translate(self, job: Job) -> Outcome:
        s = job.status
        self._active["translate"] = {
            "name": s.streamer.name, "platform": s.streamer.platform, "title": s.title, "url": s.url,
            "started": self.clock(), "stage": "translate",
            "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
        try:
            assert self.translate and job.video
            self.translate(Path(job.video))
        except Exception as e:  # noqa: BLE001 - LLM down, missing transcript: all retried
            return Outcome("error", str(e) or type(e).__name__)
        finally:
            self._active["translate"] = None
        return Outcome("done")

    def attempt_upload(self, job: Job) -> Outcome:
        s = job.status
        self._active["upload"] = {
            "name": s.streamer.name, "platform": s.streamer.platform, "title": s.title, "url": s.url,
            "started": self.clock(), "stage": "upload",
            "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
        try:
            assert self.upload and job.video
            sheet = self.uploads.get(s.streamer.id)
            if not sheet:
                return Outcome("done", "")  # switched off in the gear meanwhile
            table = Archive.from_video(Path(job.video)).require(Artifact.HIGHLIGHTS)
            link = self.upload(table, sheet, s.streamer.name, s.title or "")
        except Exception as e:  # noqa: BLE001 - API errors, timeouts, expired token: all retried
            return Outcome("error", str(e) or type(e).__name__)
        finally:
            self._active["upload"] = None
        return Outcome("done", link)

    def attempt_drive(self, job: Job) -> Outcome:
        s = job.status
        assert job.video  # a drive job is only queued for a live with a video
        folder = self.drive_folders.get(s.streamer.id)
        if folder is None:
            return Outcome("done")  # unticked in the gear meanwhile: nothing to upload
        if not self.drive:  # gone since the job was queued (the CLI was removed, then a restart)
            log.warning("drive copies are on for %s, but the drive is not available: dropped", s.streamer.name)
            return Outcome("unavailable")
        if self._awaits_chat(job):
            return Outcome("wait", "等待弹幕补抓")  # the zip gets the chat: wait until it is in or given up
        if (begin := drive_hours_begin(self.clock(), self.drive_hours)) is not None:
            return Outcome("wait", "等待网盘上传时段", retry_at=begin)  # outside the drive upload hours
        self._active["drive"] = {
            "name": s.streamer.name, "platform": s.streamer.platform, "title": s.title, "url": s.url,
            "started": self.clock(), "stage": "drive",
            "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
        try:
            copy = self.drive(Archive.from_video(Path(job.video)), folder, self._on_drive_progress)
        except DriveAuthorizationLapsed as e:
            return Outcome("lapsed", str(e))
        except DriveFailed as e:
            return Outcome("error", str(e))
        except Exception as e:  # noqa: BLE001 - a bug or a surprise: a failed attempt, not a stuck job
            log.exception("drive attempt raised: %s %s", s.streamer.name, s.url)
            return Outcome("error", f"{type(e).__name__}: {e}")
        finally:
            self._active["drive"] = None
        return Outcome("done", drive=copy)

    def attempt_flash(self, job: Job) -> Outcome:
        """One look at the live's flash transfer: after its late chat, prepare the files and
        start it (`Outcome.flash` is then saved in the job), or, once started, ask for its
        progress. Done once it is uploaded, with its flash link noted in the archive. The
        staging folder goes once the flash transfer is uploaded or cannot be made."""
        s = job.status
        assert job.video  # a flash job is only queued for a live with a video
        if self._awaits_chat(job):  # the chat goes into the flash transfer (READY waits for it too)
            return Outcome("wait", "等待弹幕补抓", retry_at=self.clock() + FLASH_WAIT_LOOK)
        archive = Archive.from_video(Path(job.video))
        root = self.settings.output_dir
        if not self.flash:  # gone since the job was queued (.env changed, then a restart)
            log.warning("flash transfers are on for %s, but no flash service is configured: dropped", s.streamer.name)
            remove_staging(archive, root)  # a flash transfer it had started is never looked at again
            return Outcome("unavailable")
        started = job.transfer
        prepared = False  # this try prepared the files: a start that cannot reach the flash service waits a retry pause
        try:
            if started is None:
                since = job.flash_offline_since
                if since is not None and self.clock() - since >= FLASH_OFFLINE_GIVE_UP:
                    remove_staging(archive, root)
                    return Outcome("waited_out", job.last_error)  # nothing prepared for a live given up on
                # Nothing is prepared or cut before the account is known to be online (asked of
                # the flash service alone: never the login interface, which only the 通知 page
                # uses). A live behind one that waits, waits with it. A waiting job whose look
                # finds the status online only notes it: its next try (the busy mark set) prepares.
                self.flash.require_online()
                if self._goes_after(job, self.load()):
                    return self._flash_offline(job, archive, "排在前面的录播在等闪传账号")
                if self._looks_only(job):
                    return Outcome("wait", "闪传账号显示在线", retry_at=self.clock(), online=True)
                files = prepare(archive, root, ffmpeg=self.settings.ffmpeg)
                prepared = True
                started = self.flash.start(files.files, files.name)
                log.info("flash transfer started: %s %s (%d files)", s.streamer.name, s.url, len(files.files))
                return Outcome("wait", "闪传上传中", retry_at=self.clock(), flash=started, served=True)
            progress = self.flash.progress(started.id)
        except FlashUnavailable as e:  # waits for the account: not a failed try
            return self._flash_offline(job, archive, str(e), pause=prepared)
        except FlashFailed as e:
            return self._flash_failed(archive, str(e))
        except Exception as e:  # noqa: BLE001 - a bug or a surprise: a failed try, not a stuck job
            # Its kind and its text without addresses, no traceback: a flash service's error
            # can name its address, or a flash link.
            reason = without_addresses(f"{type(e).__name__}: {e}")
            log.warning("flash attempt raised: %s %s: %s", s.streamer.name, s.url, reason)
            return self._flash_failed(archive, reason)
        if progress.failed:
            return self._flash_failed(archive, f"{progress.failed} 个文件上传失败", served=True)
        if not progress.finished:
            moved_at = job.flash_moved_at  # None: not looked at since a restart from an older queue file
            if moved_at is not None and progress.sent == job.flash_sent and self.clock() - moved_at >= FLASH_STALL:
                return self._flash_failed(archive, f"上传 {FLASH_STALL // 60} 分钟没有进展", served=True)
            self._active["flash"] = {
                "name": s.streamer.name, "platform": s.streamer.platform, "title": s.title, "url": s.url,
                "started": started.made.timestamp(), "stage": "flash",
                "percent": round(progress.sent / progress.total * 100, 1) if progress.total else None,
                "downloaded": progress.sent, "total": progress.total or None, "speed": None, "eta": None}
            return Outcome("wait", "闪传上传中", retry_at=self.clock() + FLASH_PROGRESS_LOOK, sent=progress.sent,
                           served=True)
        try:
            note_flash_link(archive, started)
        except Exception:  # noqa: BLE001 - the page loses the link, the message still carries it
            log.exception("noting the flash link in the archive failed: %s %s", s.streamer.name, s.url)
        remove_staging(archive, root)
        return Outcome("done", flash=started, served=True)

    def _flash_failed(self, archive: Archive, reason: str, served: bool = False) -> Outcome:
        """A try at a flash transfer failed: its staging folder goes (a new try prepares the
        files again). `served`: the flash service answered its progress."""
        remove_staging(archive, self.settings.output_dir)
        return Outcome("error", reason, served=served)

    def _flash_offline(self, job: Job, archive: Archive, reason: str, pause: bool = False) -> Outcome:
        """The flash account is offline or the flash service cannot be reached: the job waits
        and is looked at again in FLASH_OFFLINE_LOOK, or after the retry pause when this try
        prepared the files (`pause`: a status that says online while the start cannot reach
        the flash service would prepare them, and cut a big video, at every look);
        FLASH_OFFLINE_GIVE_UP after it first found it so, it is given up on. A started flash
        transfer keeps its staging folder (the flash service reads it when it goes on); any
        other is removed (prepared again later)."""
        now = self.clock()
        since = job.flash_offline_since if job.flash_offline_since is not None else now
        over = now - since >= FLASH_OFFLINE_GIVE_UP
        if over or job.transfer is None:
            remove_staging(archive, self.settings.output_dir)
        if over:
            return Outcome("waited_out", reason)
        look = self.retry_interval if pause else FLASH_OFFLINE_LOOK
        return Outcome("offline", reason, retry_at=min(now + look, since + FLASH_OFFLINE_GIVE_UP))

    @staticmethod
    def _looks_only(job: Job) -> bool:
        """A try at this flash job (no flash transfer started) only looks at the account: it
        waits for it, and its last look did not find the status online."""
        return job.flash_offline_since is not None and not job.flash_status_online

    @staticmethod
    def _goes_after(job: Job, jobs: list[Job]) -> bool:
        """Another live goes before this flash job (no flash transfer started): its started
        flash transfer is in the queue (one at a time; that one goes on first), or a live
        processed earlier waits for the flash account (the lives go on in their order)."""
        return any(j.stage == "flash" and j.key != job.key
                   and (j.transfer_id or (j.flash_offline_since is not None and j.queued_at < job.queued_at))
                   for j in jobs)

    def _on_drive_progress(self, sent: int, total: int | None) -> None:
        """The drive's progress (runs in the drive thread)."""
        if active := self._active["drive"]:
            active.update(downloaded=sent, total=total,
                          percent=round(sent / total * 100, 1) if total else None)

    def _on_progress(self, d: dict[str, Any]) -> None:
        """yt-dlp progress hook (runs in the download thread)."""
        active = self._active["download"]
        if active is None or d.get("status") != "downloading":
            return
        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        active.update(
            downloaded=done,
            total=total,
            percent=round(done / total * 100, 1) if total else None,
            speed=d.get("speed"),
            eta=d.get("eta"),
        )

    def finish(self, job: Job, outcome: Outcome) -> None:
        """Record an attempt's outcome and send its message. Once nothing is left for the
        live (every stage done or given up), report it ready with what it has; a live with a
        flash job is reported ready by its end, one with only a drive job by `send_waiting_ready`."""
        if job.stage == "drive":
            self._finish_drive(job, outcome)
            return
        if job.stage == "flash":
            self._finish_flash(job, outcome)
            return
        self._finish(job, outcome)
        video = job.video or (str(outcome.result.video) if outcome.result and outcome.result.video else None)
        if video and not any(j.stream_key == job.stream_key for j in self.load()):
            self._send_ready(job, Path(video))
        self.send_waiting_ready()

    def _send_ready(self, job: Job, video: Path, flash: StartedTransfer | None = None) -> None:
        """READY with the site's links; the source record sheet is told the 全部下载 link it
        carries (none without SITE_URL: then nothing is told). With the live's uploaded flash
        transfer `flash`, READY carries its flash link in place of 全部下载 (no short download
        link is made) and says until when it works; the sheet is told the flash link."""
        if flash:
            links = flash_ready_links(video, self.site_url, self.settings.output_dir, flash.link)
            told = {"link": flash.link, "expires": flash.expires.timestamp(), "flash": True}
            self._send(EventKind.READY, job, f"{ready_detail(video)}\n{flash_expiry_line(flash)}", links)
        else:
            short = (self.download_link(Downloads(self.settings.output_dir).ref(video.parent))
                     if self.site_url and self.download_link else None)
            links = ready_links(video, self.site_url, self.settings.output_dir, short)
            told = {"link": links[0].url, "expires": short.expires if short else None} if links else {}
            self._send(EventKind.READY, job, ready_detail(video), links)
        if self.source_record and told:
            s = job.status
            try:
                self.source_record.processed(
                    s.streamer, date=Archive.from_video(video).date_folder, title=s.title or "",
                    stream_id=s.stream_id or s.url or "", **told)
            except Exception:  # noqa: BLE001 - the sheet is bookkeeping: it never holds up a message
                log.exception("telling the source record sheet failed: %s", s.streamer.name)

    def send_waiting_ready(self) -> None:
        """Report ready each live that has a drive job and was not reported yet, unless its
        chat is still awaited: READY waits for a late chat, never for the upload."""
        jobs = self.load()
        due = [j for j in jobs if j.stage == "drive" and not j.ready_sent and not self._awaits_chat(j)]
        if not due:
            return
        for j in due:
            j.ready_sent = True
        self.save(jobs)
        for j in due:
            assert j.video  # a drive job is only queued for a live with a video
            self._send_ready(j, Path(j.video))

    def _awaits_chat(self, job: Job) -> bool:
        """The live's chat is still in the chat retry queue (neither downloaded nor given up)."""
        chats = PendingChats.from_settings(self.settings)
        return bool(chats and job.video and chats.holds(Path(job.video).parent))

    def _finish(self, job: Job, outcome: Outcome) -> None:
        jobs = self.load()  # reload: on_ended may have added jobs meanwhile
        current = next((j for j in jobs if j.key == job.key), job)
        rest = [j for j in jobs if j.key != job.key]
        now = self.clock()
        current.attempts += 1
        current.last_error = outcome.message

        name = job.status.streamer.name
        if outcome.kind == "done" and current.stage == "analyze":
            log.info("analyzed: %s %s", name, outcome.message)
            assert job.video  # analysis only runs on a downloaded video
            self._send(EventKind.ANALYZED, job, top_segments_detail(Archive.from_video(Path(job.video))))
            if job.status.streamer.id not in self.uploads:
                self._settle(current, rest)
            elif not self.upload:
                log.warning("upload to Tencent Docs is on for %s, but no credentials in .env: skipped", name)
                self._settle(current, rest)
            else:
                self.advance(current, rest, "upload", now)
            return
        if outcome.kind == "done" and current.stage == "upload":
            self._settle(current, rest)
            if outcome.message:
                log.info("uploaded: %s %s", name, outcome.message)
                self._send(EventKind.UPLOADED, job, "", (Link(SHEET_LABEL, outcome.message),))
            return
        if outcome.kind == "done" and current.stage == "translate":
            log.info("translated: %s %s", name, current.video)
            self._settle(current, rest)
            return
        if outcome.kind == "done" and current.stage == "transcribe":
            log.info("transcribed: %s %s", name, current.video)
            # Analysis and translation both only need the transcript: one job each, side by side.
            follow = [stage for stage, fn in (("analyze", self.analyze), ("translate", self.translate)) if fn]
            jobs = []
            for stage in follow:
                j = Job.from_dict(current.to_dict())
                j.stage = stage  # type: ignore[assignment]
                j.attempts, j.errors, j.last_error, j.next_try = 0, 0, "", now
                jobs.append(j)
            self._settle(current, rest + jobs)
            for stage in follow:
                self._wake[stage].set()
            return
        if outcome.kind == "done":
            log.info("auto-download done: %s %s", name, outcome.result and outcome.result.video)
            # Set on the passed-in `job` as well as on `current`, its copy from the queue file:
            # when no stage follows the download, `finish` sends 「全部处理完成」 with `job`, not with
            # `current`, and that message must show the replay's cover too.
            current.replay_cover = job.replay_cover = outcome.result.info.cover if outcome.result else ""
            if outcome.result:  # what the owner checks at a Twitch live; never the address itself
                info = outcome.result.info
                log.info("replay of %s downloaded, its metadata offered %s", name,
                         "a cover" if info.cover else
                         "only a placeholder, taken as no cover" if info.cover_placeholder else "no cover")
            links: tuple[Link, ...] = ()
            if self.site_url and outcome.result:
                ref = Downloads(self.settings.output_dir).ref(outcome.result.output_dir)
                links = (Link(PAGE_LABEL, archive_link(self.site_url, ref)),)
            self._send(EventKind.DOWNLOADED, job, success_detail(outcome), links)
            if outcome.prune_error:
                self._send(EventKind.PRUNE_FAILED, job, "删除这位主播超出保留数量的旧录播时出错，旧录播没有删掉；"
                           f"这场新录播不受影响。\n```{outcome.prune_error[:1500]}```")
            video = outcome.result.video if outcome.result else None
            if video:
                current.video = str(video)
            if self.transcribe and video:
                self.advance(current, rest, "transcribe", now)
            else:
                self._settle(current, rest)
            return
        if outcome.kind == "unpublished":
            if current.short_live:
                log.info("auto-download dropped, replay of a short live not published: %s", name)
                self._settle(current, rest)
                return
            if now - current.queued_at >= self.give_up:
                log.info("auto-download given up, replay never published: %s", name)
                self._settle(current, rest)
                return
            if not current.unpublished_notice_sent:
                self._send(EventKind.REPLAY_UNPUBLISHED, job,
                           f"回放未公开，{self.give_up / 3600:g} 小时内公开会自动下载。")
                current.unpublished_notice_sent = True
            log.info("auto-download waiting for the replay to be published: %s", name)
        elif outcome.kind == "wait":
            if now - current.queued_at >= self.give_up:
                self._settle(current, rest)
                hours = self.give_up / 3600
                self._send(EventKind.DOWNLOAD_FAILED, job,
                           f"下播 {hours:g} 小时后回放仍未就绪，放弃。\n最后状态：{outcome.message}")
                return
            log.info("auto-download waiting (%s): %s", outcome.message, name)
        else:
            current.errors += 1
            log.warning("auto-download failed (%d/%d): %s: %s",
                        current.errors, self.max_errors, name, outcome.message)
            if current.errors >= self.max_errors:
                self._settle(current, rest)
                if current.stage == "transcribe":
                    self._send(EventKind.TRANSCRIBE_FAILED, job,
                               f"影片已下载，但试了 {current.attempts} 次字幕转写仍失败：\n"
                               f"```{current.last_error[:1500]}```")
                elif current.stage == "translate":
                    self._send(EventKind.TRANSLATE_FAILED, job,
                               f"字幕已转写，但试了 {current.attempts} 次中文翻译仍失败：\n"
                               f"```{current.last_error[:1500]}```")
                elif current.stage == "upload":
                    self._send(EventKind.UPLOAD_FAILED, job,
                               f"路灯表已生成，但试了 {current.attempts} 次上传腾讯文档仍失败：\n"
                               f"```{current.last_error[:1500]}```")
                elif current.stage == "analyze":
                    self._send(EventKind.ANALYZE_FAILED, job,
                               f"影片和字幕都好了，但试了 {current.attempts} 次路灯表分析仍失败：\n"
                               f"```{current.last_error[:1500]}```")
                else:
                    self._send(EventKind.DOWNLOAD_FAILED, job, failure_detail(current, self.settings))
                return
        current.next_try = now + self.retry_interval
        self.save(rest + [current])

    def _finish_drive(self, job: Job, outcome: Outcome) -> None:
        """A drive copy that is uploaded is told with 网盘上传完成 (the drive link, or the zip's
        path and why there is no link). A failed upload is retried; only its first failure
        and its give-up are told. READY goes first when the live was not reported ready yet
        (the job's turn came before `send_waiting_ready` saw its chat was no longer awaited)."""
        jobs = self.load()
        current = next((j for j in jobs if j.key == job.key), job)
        rest = [j for j in jobs if j.key != job.key]
        name = job.status.streamer.name
        now = self.clock()
        current.uploading = False  # the attempt is over, whatever its outcome
        if outcome.kind == "wait":  # the chat or the upload hours are awaited: neither an attempt nor an error
            current.next_try = outcome.retry_at or now + self.retry_interval
            self.save(rest + [current])
            return
        first = current.attempts == 0 and current.lapsed_since is None  # its first try that is not a wait
        if outcome.kind != "lapsed":  # a lapsed try is neither an attempt nor an error
            current.attempts += 1
            current.lapsed_since = None
        current.last_error = outcome.message
        copy = outcome.drive
        keep = False
        assert current.video  # a drive job is only queued for a live with a video
        video = Path(current.video)
        events: list[tuple[EventKind, str, tuple[Link, ...]]] = []
        drive_link = ""  # the link 网盘上传完成 carries: told to the source record sheet after it
        ready_due = not current.ready_sent  # READY goes first, before the events below
        current.ready_sent = True
        if outcome.kind == "lapsed":
            log.warning("drive copy not uploaded, the drive's authorization lapsed: %s: %s", name, outcome.message)
            if current.lapsed_since is None:
                current.lapsed_since = now
            keep = now - current.lapsed_since < DRIVE_LAPSE_GIVE_UP
            if not keep:
                events.append((EventKind.DRIVE_FAILED, f"网盘授权失效后等了 {DRIVE_LAPSE_GIVE_UP / 3600:g} 小时"
                               "仍没有重新授权，放弃上传这场录播。", ()))
            elif first:
                events.append((EventKind.DRIVE_FAILED, "网盘授权已失效，这场录播没能上传；重新授权后会自动继续上传"
                               f"（最多等 {DRIVE_LAPSE_GIVE_UP / 3600:g} 小时）。", ()))
            if not self._lapse_alerted:  # once per lapse, whichever job finds it
                self._lapse_alerted = True
                events.append((EventKind.DRIVE_AUTHORIZATION_LAPSED,
                               f"夸克网盘的授权失效了，网盘上传都在等待。在服务器上重新授权：\n`{DRIVE_LOGIN_COMMAND}`", ()))
        elif outcome.kind == "unavailable":
            events.append((EventKind.DRIVE_FAILED, "网盘不可用（服务器上没有夸克网盘的命令行工具），"
                           "这场录播的网盘上传已取消；录播之后会按保留数量照常清理。", ()))
        elif outcome.kind == "error":
            self._lapse_alerted = False  # a try that is not lapsed ends the lapse: a later one is new
            current.errors += 1
            log.warning("drive copy not uploaded (%d/%d): %s: %s", current.errors, self.max_errors, name, outcome.message)
            keep = current.errors < self.max_errors
            if not keep:
                events.append((EventKind.DRIVE_FAILED, f"试了 {current.attempts} 次网盘上传仍失败，放弃上传这场录播：\n"
                               f"```{outcome.message[:1500]}```", ()))
            elif first:
                events.append((EventKind.DRIVE_FAILED, f"网盘上传失败，之后会自动重试：\n```{outcome.message[:1500]}```", ()))
        elif copy:
            log.info("drive copy uploaded: %s %s", name, copy.path)
            self._lapse_alerted = False  # a try that is not lapsed ends the lapse: a later one is new
            if copy.link:  # the link leads to the zip: the live's date is enough
                detail = f"日期：{live_date(video)}"
                drive_link = copy.link
            else:  # without a link, the path is the only way to the zip
                detail = f"网盘路径：`{copy.path}`\n没有网盘链接：{copy.reason}"
            events.append((EventKind.DRIVE_UPLOADED, detail,
                           (Link(DRIVE_LINK_LABEL, copy.link),) if copy.link else ()))
        current.next_try = now + self.retry_interval
        self.save(rest + [current] if keep else rest)
        if ready_due:
            self._send_ready(job, video)
        for kind, detail, links in events:
            self._send(kind, job, detail, links)
        if self.source_record and drive_link:
            s = job.status
            try:
                self.source_record.uploaded(s.streamer, date=Archive.from_video(video).date_folder, title=s.title or "",
                                            link=drive_link, stream_id=s.stream_id or s.url or "")
            except Exception:  # noqa: BLE001 - the sheet is bookkeeping: it never holds up a message
                log.exception("telling the source record sheet failed: %s", name)

    def _finish_flash(self, job: Job, outcome: Outcome) -> None:
        """A flash job's look: while it waits (its late chat, its upload) the job stays, with
        the flash transfer it started and how far its upload moved. A failed try is counted
        and tried again after the retry interval with a new flash transfer; at `max_errors`
        the job is given up on (`_end_flash` with the owner's alert). Else it ends, and READY
        goes out, with the flash link when the flash transfer is uploaded, else as for a live
        without one. While the flash account is offline the job waits for it, neither an
        attempt nor an error, and the owner is reminded (`_flash_reminder`); after
        FLASH_OFFLINE_GIVE_UP it is given up on without the alert of a failure. A look that
        the flash service served ends every live's waiting; one that merely finds the status
        online lets this job's next try prepare and start."""
        jobs = self.load()
        current = next((j for j in jobs if j.key == job.key), job)
        rest = [j for j in jobs if j.key != job.key]
        current.cutting = False  # the attempt is over, whatever its outcome
        current.flash_status_online = outcome.online
        name = job.status.streamer.name
        now = self.clock()
        if outcome.served:
            self._account_back([current, *rest], now)
        if outcome.kind == "offline":  # neither an attempt nor an error
            self._active["flash"] = None
            current.last_error = without_addresses(outcome.message)
            current.flash_moved_at = None  # a started upload's FLASH_STALL counts again after the scan
            if current.flash_offline_since is None:
                current.flash_offline_since = now
                log.warning("flash transfer waits for the flash account: %s %s: %s", name, current.status.url,
                            current.last_error)
            current.next_try = outcome.retry_at if outcome.retry_at is not None else now + FLASH_OFFLINE_LOOK
            queue = rest + [current]
            reminder = self._flash_reminder(queue, now)
            self.save(queue)
            if reminder:
                self._tell_owner(reminder, "of the flash account offline")
            return
        if outcome.kind == "waited_out":  # the reminders said why: no alert of a failure
            log.warning("flash transfer given up on after waiting %g hours for the flash account: %s %s",
                        FLASH_OFFLINE_GIVE_UP / 3600, name, current.status.url)
            self._active["flash"] = None
            self._end_flash(current, rest)
            return
        if outcome.kind == "wait":  # neither an attempt nor an error
            if outcome.flash:  # just started: nothing sent yet
                current.transfer = outcome.flash
                current.flash_sent, current.flash_moved_at = 0, now
            elif outcome.sent is not None and (outcome.sent != current.flash_sent or current.flash_moved_at is None):
                current.flash_sent, current.flash_moved_at = outcome.sent, now
            current.next_try = outcome.retry_at if outcome.retry_at is not None else now
            self.save(rest + [current])
            return
        self._active["flash"] = None
        if outcome.kind == "error":  # a failed try: the next one starts a new flash transfer
            current.attempts += 1
            current.errors += 1
            current.last_error = without_addresses(outcome.message)
            current.transfer = None
            current.flash_sent = current.flash_moved_at = None
            log.warning("flash transfer failed (%d/%d): %s: %s", current.errors, self.max_errors, name,
                        current.last_error)
            if current.errors < self.max_errors:
                current.next_try = self.clock() + self.retry_interval
                self.save(rest + [current])
                return
            log.warning("flash transfer given up on: %s %s", name, current.status.url)
            self._end_flash(current, rest, alert=flash_given_up_message(current, self.settings.output_dir))
            return
        if outcome.kind == "done":
            log.info("flash transfer uploaded: %s %s", name, current.status.url)
        self._end_flash(current, rest, outcome.flash if outcome.kind == "done" else None)

    def _end_flash(self, job: Job, rest: list[Job], flash: StartedTransfer | None = None,
                   alert: str | None = None) -> None:
        """The flash job leaves the queue (`rest`: the queue without it) and its live's READY
        goes out: with the flash link of `flash`, the flash transfer uploaded; without it, as
        for a live without a flash transfer (the short download link). `alert`: the owner's
        operator alert, sent after READY (a flash transfer given up on)."""
        self.save(rest)
        assert job.video  # a flash job is only queued for a live with a video
        self._send_ready(job, Path(job.video), flash)
        if alert:
            self._tell_owner(alert, f"of a flash transfer given up on: {job.status.streamer.name}")

    def _tell_owner(self, text: str, what: str) -> None:
        """The owner's operator alert, when there is one (on the event loop's thread)."""
        if self.alert:
            try:
                self.alert(text)
            except Exception:  # noqa: BLE001 - an alert never holds up the pipeline
                log.exception("the owner was not told %s", what)

    @staticmethod
    def _account_back(jobs: list[Job], now: float) -> None:
        """The flash service served the stage again: no flash job waits for the account any
        more, and each that did is looked at now (the order of the lives decides which goes
        first). When they were reminded stays noted: a new waiting within the day is not
        reminded again."""
        back = [j for j in jobs if j.stage == "flash" and j.flash_offline_since is not None]
        for j in back:
            j.flash_offline_since = None
            j.flash_status_online = False
            j.next_try = min(j.next_try, now)
        if back:
            log.info("the flash account is online again: %d flash transfer(s) go on", len(back))

    def _flash_reminder(self, jobs: list[Job], now: float) -> str | None:
        """The reminder of the waiting for the flash account, when it is due: at the first
        wait, then at most once every FLASH_REMINDER_EVERY while any live a reminder covered
        is in the queue, whatever the account did in between. One reminder covers every
        waiting live; its time is noted in each (a restart keeps it)."""
        flash = [j for j in jobs if j.stage == "flash"]
        waiting = [j for j in flash if j.flash_offline_since is not None]
        last = max((j.flash_reminded_at for j in flash if j.flash_reminded_at is not None), default=None)
        if last is not None and now - last < FLASH_REMINDER_EVERY:
            for j in waiting:  # a live that starts to wait now is covered by the last reminder
                j.flash_reminded_at = j.flash_reminded_at or last
            return None
        for j in waiting:
            j.flash_reminded_at = now
        return flash_offline_reminder(len(waiting), self.site_url)

    def flash_waiting(self) -> int:
        """How many lives wait for the flash account (the 通知 page's 「闪传账号」 block)."""
        return sum(1 for j in self.load() if j.stage == "flash" and j.flash_offline_since is not None)

    def _settle(self, job: Job, jobs: list[Job]) -> None:
        """Save `jobs`, the queue without `job`. When that leaves nothing for its live, its
        drive job and its flash job are queued in the same save, for a live that goes to the
        drive or to a flash transfer (so a prune never sees the archive without a job)."""
        if any(j.stream_key == job.stream_key for j in jobs):
            self.save(jobs)
            return
        now = self.clock()
        to_flash = self._goes_to_flash(job)
        follow: list[Job] = []
        if self._goes_to_drive(job):
            drive = self._follower(job, "drive", now)
            drive.ready_sent = to_flash  # the flash job's end sends READY
            follow.append(drive)
        if to_flash:
            flash = self._follower(job, "flash", now)
            flash.queued_at = now  # flash transfers go in the order the lives were processed
            follow.append(flash)
        self.save(jobs + follow)
        for j in follow:
            self._wake[j.stage].set()

    @staticmethod
    def _follower(job: Job, stage: str, now: float) -> Job:
        """A copy of the live's last processing job for a stage after the processing."""
        follower = Job.from_dict(job.to_dict())
        follower.stage = stage  # type: ignore[assignment]
        follower.attempts, follower.errors, follower.last_error, follower.next_try = 0, 0, "", now
        return follower

    def _goes_to_flash(self, job: Job) -> bool:
        """A ticked streamer's live with a video, when a flash service is configured."""
        if job.status.streamer.id not in self.flash_streamers or not job.video:
            return False
        if not self.flash:
            log.warning("flash transfers are on for %s, but no flash service is configured: skipped",
                        job.status.streamer.name)
            return False
        return Archive.from_video(Path(job.video)).has(Artifact.VIDEO)

    def _goes_to_drive(self, job: Job) -> bool:
        """A ticked streamer's live with a video, when the drive is available."""
        if job.status.streamer.id not in self.drive_folders or not job.video:
            return False
        if not self.drive:
            log.warning("drive copies are on for %s, but the drive is not available: skipped",
                        job.status.streamer.name)
            return False
        return Archive.from_video(Path(job.video)).has(Artifact.VIDEO)

    def advance(self, job: Job, rest: list[Job], stage: str, now: float) -> None:
        """Move a job to its next stage with fresh retry counters, and wake that worker."""
        job.stage = stage  # type: ignore[assignment]
        job.attempts, job.errors, job.last_error = 0, 0, ""
        job.next_try = now
        self.save(rest + [job])
        self._wake[stage].set()

    def _send(self, kind: EventKind, job: Job, detail: str, links: tuple[Link, ...] = ()) -> None:
        """Send one message of the job's live, which has ended: it shows the ended live's
        picture (`StreamStatus.as_ended`), with the replay's cover once the replay is downloaded."""
        if self.notify:
            self.notify(StreamEvent(kind, job.status.as_ended(job.replay_cover), detail=detail, links=links))

    async def retry_chats(self) -> None:
        """Retry the late chats that are due, then report ready the lives that waited for theirs."""
        queue = PendingChats.from_settings(self.settings)
        due = queue.next_due() if queue else None
        if queue and due is not None and due <= time.time():
            await asyncio.to_thread(queue.retry, self.streamers, self.settings)
        self.send_waiting_ready()

    # ---- retention ----

    def prune(self, protect: Path, keep: int) -> list[Path]:
        """Delete the creator's oldest archives beyond `keep`, never `protect` (the one just
        downloaded) nor one that still has a job in the queue, at any stage, and forget their
        queued chats. Returns what was deleted."""
        processing = [Path(j.video).parent for j in self.load() if j.video]
        removed = Downloads(self.settings.output_dir).prune(protect, keep, processing)
        if removed and (queue := PendingChats.from_settings(self.settings)):
            for folder in removed:
                queue.drop_under(folder)
        return removed


DRIVE_HOURS_ZONE = timezone(timedelta(hours=8))  # Hong Kong, whatever the server's clock is set to


def drive_hours_begin(now: float, hours: tuple[int, int] | None) -> float | None:
    """None when a drive copy's upload may start at `now` (inside the drive upload hours, or
    there are none); else the time they next begin."""
    if hours is None:
        return None
    start, end = hours
    local = datetime.fromtimestamp(now, DRIVE_HOURS_ZONE)
    if start <= local.hour < end:
        return None
    begin = local.replace(hour=start, minute=0, second=0, microsecond=0)
    return (begin if local.hour < start else begin + timedelta(days=1)).timestamp()


def ready_detail(video: Path) -> str:
    """What a finished stream has (✅ / ❌)."""
    present = Archive.from_video(video).present()
    return " · ".join(f"{name} {'✅' if artifact in present else '❌'}" for name, artifact in READY_ARTIFACTS)


def live_date(video: Path) -> str:
    """The date a live's archive is filed under, as a message shows it (`2026-11-18`); a date
    folder that is not `YYYYMMDD` is shown as it is."""
    date = Archive.from_video(video).date_folder
    return f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 and date.isdigit() else date


@dataclass(frozen=True)
class ShortDownloadLink:
    """A short download link the dashboard made for one archive."""

    path: str  # the address without the site's, `/d/<code>`
    expires: float  # when it stops working (epoch seconds)


def ready_links(video: Path, site_url: str, output_dir: Path,
                short: ShortDownloadLink | None = None) -> tuple[Link, ...]:
    """With SITE_URL, the 全部下载 zip of a finished stream and its page; else none.
    `short` makes the zip's address that short download link, which downloads without
    logging in (the page still asks for the login)."""
    if not site_url:
        return ()
    ref = Downloads(output_dir).ref(video.parent)
    zip_path = short.path if short else f"/api/archives/zip?folder={quote(ref, safe='')}"
    return (Link("全部下载（影片、弹幕、中日字幕 SRT）", site_url.rstrip("/") + zip_path),
            Link(PAGE_LABEL, archive_link(site_url, ref)))


def flash_ready_links(video: Path, site_url: str, output_dir: Path, flash_link: str) -> tuple[Link, ...]:
    """READY's links when the live's flash transfer is uploaded: its flash link, then, with
    SITE_URL, the archive's page."""
    page = (Link(PAGE_LABEL, archive_link(site_url, Downloads(output_dir).ref(video.parent))),) if site_url else ()
    return (Link(FLASH_LINK_LABEL, flash_link), *page)


def without_addresses(reason: str) -> str:
    """A flash transfer's reason with every address left out (and cut to REASON_KEPT)."""
    return ADDRESS.sub("（地址略）", reason)[:REASON_KEPT]


def flash_given_up_message(job: Job, output_dir: Path) -> str:
    """The owner's operator alert for a flash job given up on (Chinese, like the other operator
    messages): the streamer, the live and the last try's reason, and the command that makes
    the flash transfer by hand. It carries no link: an address in the reason is left out."""
    s = job.status
    assert job.video  # a flash job is only queued for a live with a video
    video = Path(job.video)
    reason = without_addresses(job.last_error)
    return "\n".join([
        f"QQ 闪传试了 {job.errors} 次仍失败，已放弃；「全部处理完成」已照常发出（不带闪传链接）",
        f"配信者：{s.streamer.name}",
        f"日期：{live_date(video)}",
        f"直播：{s.title or '（无标题）'}",
        f"原因：{reason}",
        f"手动做闪传：`{FLASH_BY_HAND_COMMAND} \"{Downloads(output_dir).ref(video.parent)}\"`",
    ])


def flash_offline_reminder(waiting: int, site_url: str) -> str:
    """The owner's reminder that lives wait for the flash account (Chinese, like the other
    operator messages): how many, how long at most, and where to scan (the 通知 page, with
    SITE_URL). It goes to no QQ group."""
    where = f"到通知页扫码登录闪传账号：{site_url.rstrip('/')}/notify" if site_url else "到通知页扫码登录闪传账号"
    return "\n".join([
        "闪传账号掉线或闪传服务连不上",
        f"{waiting} 场录播在等闪传；每场最多等 {FLASH_OFFLINE_GIVE_UP / 3600:g} 小时，"
        "之后「全部处理完成」改带短下载链接发出",
        where,
    ])


def flash_expiry_line(flash: StartedTransfer) -> str:
    """READY's line saying until when the flash link works: its day in Hong Kong, written as
    the source record sheet writes dates (`2026/12/7`)."""
    day = flash.expires.astimezone(DRIVE_HOURS_ZONE)
    return f"闪传链接有效至 {day.year}/{day.month}/{day.day}"


READY_ARTIFACTS = [("影片", Artifact.VIDEO), ("弹幕", Artifact.CHAT), ("日文字幕", Artifact.TRANSCRIPT),
                   ("中文字幕", Artifact.CHINESE_TRANSCRIPT), ("路灯表", Artifact.HIGHLIGHTS)]  # in the message's order
PAGE_LABEL = "录播页面"
FLASH_LINK_LABEL = "闪传链接（影片、弹幕、中日字幕 SRT、路灯表）"
DRIVE_LINK_LABEL = "网盘链接"
DRIVE_LAPSE_GIVE_UP = 72 * 3600  # a drive job waits this long for the owner to authorize again
# A flash transfer's progress is asked for this often while it uploads: a 7.7 GB video took
# five minutes on the server, and READY waits for the end.
FLASH_PROGRESS_LOOK = 10
# A flash job waiting (for its live's late chat) is looked at again this often: READY waits
# for it, so the wait should not outlast the chat by more than a minute.
FLASH_WAIT_LOOK = 60
# An upload whose bytes sent have not moved for this long counts as a failed try: a 7.7 GB
# video went up in five minutes on the server, so half an hour without a byte is no slow line
# but a hung flash transfer, which would otherwise hold READY for ever. The command's limit too.
FLASH_STALL = STALL_SECONDS
# The flash account offline (or the flash service unreachable): a flash job waits for it,
# looked at this often (the scan should not wait long for the next look), for at most this
# long from its first wait, like the drive's DRIVE_LAPSE_GIVE_UP (READY then goes out with the
# short download link); the owner is reminded at the first wait and then this often.
FLASH_OFFLINE_LOOK = 60
FLASH_OFFLINE_GIVE_UP = 72 * 3600
FLASH_REMINDER_EVERY = 24 * 3600
# What the owner runs on the server to make a flash transfer given up on by hand (the alert
# adds the archive's folder reference).
FLASH_BY_HAND_COMMAND = "docker compose exec kiri /app/.venv/bin/python -m backend.flash upload"
# Left out of a flash transfer's reason before it is logged, kept in the queue (shown in the
# members' queue view) or told in an alert: a flash link, the flash service's addresses.
ADDRESS = re.compile(r"(?:https?|wss?)://\S+", re.IGNORECASE)
REASON_KEPT = 500  # characters of a reason kept and told
SHEET_LABEL = "腾讯文档路灯表"


def archive_link(site_url: str, ref: str) -> str:
    """The dashboard page of the archive with this folder reference (plays it, has the
    downloads). Every character but letters, digits and - . _ ~ is percent-escaped, so the
    address never ends a markdown link early; the page decodes it to open the detail view."""
    return f"{site_url.rstrip('/')}/archives#{quote(ref, safe='')}"


def success_detail(outcome: Outcome) -> str:
    result = outcome.result
    assert result is not None
    lines = []
    if result.video:
        size = result.video.stat().st_size / 1024**3 if result.video.exists() else 0
        lines.append(f"影片：`{result.video.name}`（{size:.1f} GB）")
    if result.chat:
        lines.append("弹幕：已下载")
    elif result.chat_queued:
        lines.append("弹幕：还没出来，之后自动补抓")
    elif err := result.errors.get("chat"):
        lines.append(f"弹幕：失败（{err[:300]}）")
    if outcome.pruned:
        lines.append(f"清理：删除了 {len(outcome.pruned)} 个旧档（" + "、".join(p.name for p in outcome.pruned) + "）")
    return "\n".join(lines)


def top_segments_detail(archive: Archive, n: int = 5) -> str:
    """Discord text for an archive's finished 路灯表: the best `n` segments."""
    try:
        rows = archive.highlight_rows()
    except (ArtifactMissing, ArtifactDamaged, OSError) as e:
        return f"路灯表已生成，但读不到：{e}"
    best = sorted((r for r in rows if isinstance(r.get("score"), int)), key=lambda r: -r["score"])[:n]
    lines = []
    for r in best:
        pick = (r.get("top_pick") or {}).get("title") or " / ".join(r.get("topics") or [])
        lines.append(f"{r['start']} ★{r['score']} {pick}")
    return "\n".join(lines) or "路灯表已生成（没有打分的段落）"


def failure_detail(job: Job, settings: DownloadSettings) -> str:
    text = f"试了 {job.attempts} 次仍失败：\n```{job.last_error[:1500]}```"
    error = job.last_error.lower()
    if job.status.streamer.platform == "twitcasting":
        if "login" in error:
            text += ("\n可能是 TwitCasting 的登录 cookies 过期：更新服务器上的 "
                     f"`{settings.twitcasting_cookies_file}`（见 docs/deploy.md）。")
        elif "not supported" in error:
            text += "\n不支持有合言葉（密码）或会员限定的 TwitCasting 直播。"
        return text
    if any(h in error for h in AUTH_HINTS):
        if job.status.streamer.platform == "twitch":
            text += "\n可能需要更新服务器 `.env` 里的 `TWITCH_OAUTH`。"
        else:
            text += "\n可能是 cookies 过期：更新服务器上的 `data/youtube_cookies.txt`（见 docs/deploy.md）。"
    return text


def load_auto(config: Path, drive: Drive | None = None, flash: FlashClient | None = None) -> AutoDownloader | None:
    """AutoDownloader from config.json (streamers from the roster), or None if
    `download.auto.enabled` is false. `drive`: the service's one drive (`load_quark`), so
    drive copies take turns with the other uploads through it; None: not available (the
    deploy check needs none). `flash`: the flash service's client (`load_flash`), the one
    the dashboard also uses; None: no flash service is configured."""
    cfg = json.loads(config.read_text(encoding="utf-8"))
    auto = (cfg.get("download") or {}).get("auto") or {}
    if auto.get("enabled") is False:
        return None
    default_keep = int(auto.get("keep", 6))
    # Built even when nobody auto-downloads yet: the dashboard's gear can turn it on.
    roster = load_roster(config)
    keep = roster.keep_map(default_keep)
    settings, streamers = load_settings(config)
    state = Path(auto.get("state_file") or "data/download_queue.json")
    asr_cfg = cfg.get("asr") or {}
    transcriber = None
    if asr_cfg and asr_cfg.get("enabled") is not False:
        asr = ASRSettings(
            url=asr_cfg.get("url") or ASRSettings.url,
            language=asr_cfg.get("language") or ASRSettings.language,
            vad=bool(asr_cfg.get("vad", True)),
            ffmpeg=settings.ffmpeg or "ffmpeg",
        )
        transcriber = functools.partial(transcribe, settings=asr)
    analyzer = load_analyzer(config, cfg) if transcriber else None
    ad = AutoDownloader(
        settings,
        streamers,
        keep,
        state if state.is_absolute() else REPO_ROOT / state,
        retry_interval=float(auto.get("retry_minutes", RETRY_INTERVAL / 60)) * 60,
        give_up=float(auto.get("give_up_hours", 12)) * 3600,
        max_errors=int(auto.get("max_errors", MAX_ERRORS)),
        default_keep=default_keep,
        transcribe=transcriber,
        analyze=analyzer,
        translate=load_translator(config, cfg) if transcriber else None,
        upload=load_uploader(config) if analyzer else None,  # uploads the analysis
        # None without the CLI: ticked streamers get no drive job
        drive=drive,
        drive_hours=drive_upload_hours(cfg),
        flash=flash,
    )
    ad.uploads = roster.upload_map()
    ad.drive_folders = roster.drive_map()
    ad.flash_streamers = roster.flash_streamers()
    ad.site_url = os.getenv("SITE_URL", "").strip().rstrip("/")
    return ad


def drive_upload_hours(cfg: dict[str, Any]) -> tuple[int, int] | None:
    """config.json's `drive.upload_hours`: [from, until], the hours of Hong Kong time between
    which a drive copy's upload may start; None without it. Raises on anything else."""
    hours = (cfg.get("drive") or {}).get("upload_hours")
    if hours is None:
        return None
    if not (isinstance(hours, list) and len(hours) == 2
            and all(isinstance(h, int) and not isinstance(h, bool) for h in hours) and 0 <= hours[0] < hours[1] <= 24):
        raise ValueError(f"drive.upload_hours must be [from, until] with 0 <= from < until <= 24, not {hours!r}")
    return hours[0], hours[1]


def load_uploader(config: Path) -> Callable[[Path, str, str, str], str] | None:
    """Tencent Docs credentials in .env -> (路灯表, spreadsheet id, streamer, title) -> sheet link.
    Without credentials uploads are off (logged); which streamers upload is the gear's call."""
    try:
        creds = load_credentials(config)
    except TencentDocsError as e:
        log.info("Tencent Docs upload off: %s", e)
        return None

    def run(table: Path, file_id: str, streamer: str, title: str) -> str:
        data = read_file(table, Artifact.HIGHLIGHTS)
        data.setdefault("streamer", streamer)
        data.setdefault("title", title)
        return upload_table(data, dataclasses.replace(creds, file_id=file_id),
                            sheet_title=sheet_title(data, file_date(table)))

    return run


def load_translator(config: Path, cfg: dict[str, Any]) -> Callable[[Path], Path] | None:
    """`translate` section + a usable `llm` section -> video -> Chinese subtitles."""
    t = cfg.get("translate")
    if t is None or (isinstance(t, dict) and t.get("enabled") is False):
        return None
    t = t if isinstance(t, dict) else {}
    try:
        llm = load_llm(config)
    except LLMError as e:
        log.warning("translation disabled: %s", e)
        return None
    settings = TranslateSettings(group=int(t.get("group", 80)), context=int(t.get("context", 5)))
    complete = functools.partial(chat, llm, response_format={"type": "json_object"})
    return functools.partial(translate_transcript, complete=complete, settings=settings)


def load_analyzer(config: Path, cfg: dict[str, Any]) -> Callable[[Path, str, str], Path] | None:
    """`analysis` section + a usable `llm` section -> (video, streamer, title) -> 路灯表.
    With `analysis.song_model`, a singing live's songs are named by that model through the
    search-backed call (same gateway, key and proxy switch). A broken LLM setup turns analysis off (logged) instead of stopping the service."""
    a = cfg.get("analysis")
    if a is None or (isinstance(a, dict) and a.get("enabled") is False):
        return None
    a = a if isinstance(a, dict) else {}
    try:
        llm = load_llm(config)
    except LLMError as e:
        log.warning("analysis disabled: %s", e)
        return None
    settings = AnalysisSettings(
        seg_seconds=int(a.get("segment_minutes", 5) * 60),
        context_seconds=int(a.get("context_seconds", 30)),
        min_score=int(a.get("min_score", 7)),
    )
    complete = functools.partial(chat, llm, response_format={"type": "json_object"})
    name_songs = song_naming(llm, a)

    def run(video: Path, streamer: str, title: str) -> Path:
        return analyze(video, complete, streamer=streamer, title=title, settings=settings, name_songs=name_songs)

    return run


def attach(config: Path, mon: StreamMonitor, drive: Drive | None = None,
           flash: FlashClient | None = None) -> tuple[LiveNotifier | None, AutoDownloader | None]:
    """Hook notifications and auto-download onto a monitor. Run `auto.run()` as a task.
    `drive`, `flash`: as in load_auto."""
    auto = load_auto(config, drive, flash)
    notifier = load_notifier(config, allow_empty=auto is not None)
    if notifier:
        mon.on_change.append(notifier.on_change)
    if auto and notifier:
        auto.notify = notifier.send
        auto.alert = notifier.alert
        notifier.on_ended.append(auto.on_ended)
    return notifier, auto
