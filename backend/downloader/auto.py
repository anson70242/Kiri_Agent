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
    }}

Rules:
- Not ready yet (YouTube still processing, Twitch archive not listed): retried every
  `retry_minutes` until `give_up_hours` after the stream ended.
- Any other failure: retried until `max_errors`, then a failure message on Discord.
- Success: a Discord message, then the streamer folder is trimmed to `keep` archives.
- Jobs live in `state_file`, so a restart resumes them (yt-dlp resumes partial files).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from backend.analysis import AnalysisSettings, analyze
from backend.archive import Archive, Artifact, ArtifactDamaged, ArtifactMissing, Downloads, read_file
from backend.asr import ASRSettings, transcribe
from backend.llm import LLMError, chat, load_llm
from backend.tencent_docs import (
    TencentDocsError, archive_date, load_credentials, sheet_title, upload_table)
from backend.translate import TranslateSettings
from backend.translate import translate as translate_transcript
from backend.monitor import StreamMonitor, Streamer, StreamStatus
from backend.monitor.roster import load_roster
from backend.notify import EventKind, LiveNotifier, StreamEvent, load_notifier
from backend.state_file import StateFile

from . import REPO_ROOT, download, downloader_for, load_settings
from .base import DownloadError, DownloadResult, DownloadSettings
from .pending import PendingChats

log = logging.getLogger(__name__)

NOT_READY = ("is_live", "is_upcoming", "post_live")
STAGES = ("download", "transcribe", "analyze", "translate", "upload")
AUTH_HINTS = ("member", "sign in", "cookie", "login", "subscriber", "private")


@dataclass
class Job:
    status: StreamStatus  # the stream's last live status (url, title, start time)
    queued_at: float
    next_try: float
    attempts: int = 0
    errors: int = 0  # failures other than "not ready yet"
    last_error: str = ""
    stage: Literal["download", "transcribe", "analyze", "translate", "upload"] = "download"
    video: str | None = None  # set once downloaded

    @property
    def stream_key(self) -> str:
        """One broadcast (a stream can have an analysis and a translation job at once)."""
        return f"{self.status.streamer.id}|{self.status.stream_id or self.status.url}"

    @property
    def key(self) -> str:
        return f"{self.stream_key}|{self.stage}"

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
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        return cls(**d | {"status": StreamStatus.from_dict(d["status"])})


def decode_jobs(raw: Any) -> list[Job]:
    """The download queue file's JSON -> jobs; raises if it isn't a list of jobs."""
    if not isinstance(raw, list):
        raise ValueError(f"expected a list of jobs, got {type(raw).__name__}")
    return [Job.from_dict(d) for d in raw]


@dataclass
class Outcome:
    kind: Literal["done", "wait", "error"]
    message: str = ""
    result: DownloadResult | None = None
    pruned: list[Path] = field(default_factory=list)


class AutoDownloader:
    def __init__(
        self,
        settings: DownloadSettings,
        streamers: Iterable[Streamer],
        keep: dict[str, int],  # streamer id -> archives to keep; only these auto-download
        state_file: Path,
        retry_interval: float = 15 * 60,
        give_up: float = 12 * 3600,
        max_errors: int = 3,
        default_keep: int = 6,  # for streamers without their own `keep`
        notify: Callable[[StreamEvent], None] | None = None,
        clock: Callable[[], float] = time.time,
        transcribe: Callable[[Path], Any] | None = None,  # video -> transcript; None: skip
        analyze: Callable[[Path, str, str], Path] | None = None,  # (video, streamer, title) -> table
        translate: Callable[[Path], Any] | None = None,  # video -> Chinese subtitles; None: skip
        # (路灯表, spreadsheet id, streamer, title) -> sheet link; None: no credentials, skip
        upload: Callable[[Path, str, str, str], str] | None = None,
    ):
        self.upload = upload
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
        self.site_url = ""  # the dashboard's address (SITE_URL in .env): links in messages
        self.notify = notify
        self.clock = clock
        # One worker per stage, so a long transcription never holds up downloads.
        self._wake = {stage: asyncio.Event() for stage in STAGES}
        self._active: dict[str, dict[str, Any] | None] = dict.fromkeys(STAGES)  # job + progress

    # ---- queue ----

    @property
    def state_file(self) -> Path:
        return self._queue.path

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
        job = Job(status, queued_at=now, next_try=now)
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
        times = [j.next_try for j in self.load() if stage in (None, j.stage)]
        if stage in (None, "download") and (queue := PendingChats.from_settings(self.settings)):
            if (due := queue.next_due()) is not None:
                times.append(due)
        if not times:
            return 300
        return min(300.0, max(1.0, min(times) - self.clock()))

    async def run_due(self, stage: str | None = None) -> None:
        while job := self.next_due(stage):
            outcome = await asyncio.to_thread(self.attempt, job)
            self.finish(job, outcome)

    def next_due(self, stage: str | None = None) -> Job | None:
        now = self.clock()
        due = [j for j in self.load() if j.next_try <= now and stage in (None, j.stage)]
        return min(due, key=lambda j: j.next_try) if due else None

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
        s = job.status
        try:
            dl = downloader_for(s.url or "", self.settings, self.streamers)
            url = dl.find_vod(s.url or "", s.streamer.channel, s.started_at)
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
        except DownloadError as e:
            return Outcome("error", str(e))
        finally:
            self._active["download"] = None
        if err := result.errors.get("video"):
            return Outcome("error", err, result)
        pruned = self.prune(result.output_dir, self.keep.get(s.streamer.id, 6))
        return Outcome("done", result=result, pruned=pruned)

    def attempt_transcribe(self, job: Job) -> Outcome:
        s = job.status
        self._active["transcribe"] = {
            "name": s.streamer.name, "platform": s.streamer.platform, "title": s.title, "url": s.url,
            "started": self.clock(), "stage": "transcribe",
            "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
        try:
            assert self.transcribe and job.video
            self.transcribe(Path(job.video))
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
            archive = Archive.from_video(Path(job.video))
            table = archive.file(Artifact.HIGHLIGHTS)
            if table is None:
                raise ArtifactMissing(Artifact.HIGHLIGHTS, archive.folder)
            link = self.upload(table, sheet, s.streamer.name, s.title or "")
        except Exception as e:  # noqa: BLE001 - API errors, timeouts, expired token: all retried
            return Outcome("error", str(e) or type(e).__name__)
        finally:
            self._active["upload"] = None
        return Outcome("done", link)

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
        stream (every stage done or given up), report it ready with what it has."""
        self._finish(job, outcome)
        video = job.video or (str(outcome.result.video) if outcome.result and outcome.result.video else None)
        if video and not any(j.stream_key == job.stream_key for j in self.load()):
            self._send(EventKind.READY, job, ready_detail(Path(video), self.site_url, self.settings.output_dir))

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
                self.save(rest)
            elif not self.upload:
                log.warning("upload to Tencent Docs is on for %s, but no credentials in .env: skipped", name)
                self.save(rest)
            else:
                self.advance(current, rest, "upload", now)
            return
        if outcome.kind == "done" and current.stage == "upload":
            self.save(rest)
            if outcome.message:
                log.info("uploaded: %s %s", name, outcome.message)
                self._send(EventKind.UPLOADED, job, outcome.message)
            return
        if outcome.kind == "done" and current.stage == "translate":
            log.info("translated: %s %s", name, current.video)
            self.save(rest)
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
            self.save(rest + jobs)
            for stage in follow:
                self._wake[stage].set()
            return
        if outcome.kind == "done":
            log.info("auto-download done: %s %s", name, outcome.result and outcome.result.video)
            detail = success_detail(outcome)
            if self.site_url and outcome.result:
                detail += "\n" + archive_link(self.site_url, self.settings.output_dir, outcome.result.output_dir)
            self._send(EventKind.DOWNLOADED, job, detail)
            video = outcome.result.video if outcome.result else None
            if self.transcribe and video:
                current.video = str(video)
                self.advance(current, rest, "transcribe", now)
            else:
                self.save(rest)
            return
        if outcome.kind == "wait":
            if now - current.queued_at >= self.give_up:
                self.save(rest)
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
                self.save(rest)
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
                    self._send(EventKind.DOWNLOAD_FAILED, job, failure_detail(current))
                return
        current.next_try = now + self.retry_interval
        self.save(rest + [current])

    def advance(self, job: Job, rest: list[Job], stage: str, now: float) -> None:
        """Move a job to its next stage with fresh retry counters, and wake that worker."""
        job.stage = stage  # type: ignore[assignment]
        job.attempts, job.errors, job.last_error = 0, 0, ""
        job.next_try = now
        self.save(rest + [job])
        self._wake[stage].set()

    def _send(self, kind: EventKind, job: Job, detail: str) -> None:
        if self.notify:
            self.notify(StreamEvent(kind, job.status, detail=detail))

    async def retry_chats(self) -> None:
        queue = PendingChats.from_settings(self.settings)
        if not queue:
            return
        due = queue.next_due()
        if due is not None and due <= time.time():
            await asyncio.to_thread(queue.retry, self.streamers, self.settings)

    # ---- retention ----

    def prune(self, protect: Path, keep: int) -> list[Path]:
        """Delete the creator's oldest archives beyond `keep`, never `protect` (the one just
        downloaded), and forget their queued chats. Returns what was deleted."""
        removed = Downloads(self.settings.output_dir).prune(protect, keep)
        if removed and (queue := PendingChats.from_settings(self.settings)):
            for folder in removed:
                queue.drop_under(folder)
        return removed


def ready_detail(video: Path, site_url: str, output_dir: Path) -> str:
    """What a finished stream has (✅ / ❌), and with SITE_URL the 全部下载 zip and its page."""
    archive = Archive.from_video(video)
    present = archive.present()
    lines = [" · ".join(f"{name} {'✅' if artifact in present else '❌'}" for name, artifact in READY_ARTIFACTS)]
    if site_url:
        ref = Downloads(output_dir).ref(archive.folder)
        lines += ["全部下载（影片、弹幕、中日字幕 SRT）：",
                  f"{site_url.rstrip('/')}/api/archives/zip?folder={quote(ref, safe='')}",
                  "录播页面：", archive_link(site_url, output_dir, archive.folder)]
    return "\n".join(lines)


READY_ARTIFACTS = [("影片", Artifact.VIDEO), ("弹幕", Artifact.CHAT), ("日文字幕", Artifact.TRANSCRIPT),
                   ("中文字幕", Artifact.CHINESE_TRANSCRIPT), ("路灯表", Artifact.HIGHLIGHTS)]  # in the message's order


def archive_link(site_url: str, output_dir: Path, folder: Path) -> str:
    """The archive's page on the dashboard (plays it, has the downloads). The folder is
    escaped like the page's encodeURIComponent, so its detail view opens."""
    ref = Downloads(output_dir).ref(folder)
    return f"{site_url.rstrip('/')}/archives#{quote(ref, safe=SAFE_URI_CHARS)}"


SAFE_URI_CHARS = "!'()*"  # with A-Z a-z 0-9 - _ . ~: what encodeURIComponent leaves alone


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
        rows = archive.read_highlights().get("rows") or []
    except (ArtifactMissing, ArtifactDamaged, OSError) as e:
        return f"路灯表已生成，但读不到：{e}"
    best = sorted((r for r in rows if isinstance(r.get("score"), int)), key=lambda r: -r["score"])[:n]
    lines = []
    for r in best:
        pick = (r.get("top_pick") or {}).get("title") or " / ".join(r.get("topics") or [])
        lines.append(f"{r['start']} ★{r['score']} {pick}")
    return "\n".join(lines) or "路灯表已生成（没有打分的段落）"


def failure_detail(job: Job) -> str:
    text = f"试了 {job.attempts} 次仍失败：\n```{job.last_error[:1500]}```"
    if any(h in job.last_error.lower() for h in AUTH_HINTS):
        if job.status.streamer.platform == "twitch":
            text += "\n可能需要更新服务器 `.env` 里的 `TWITCH_OAUTH`。"
        else:
            text += "\n可能是 cookies 过期：更新服务器上的 `data/youtube_cookies.txt`（见 docs/deploy.md）。"
    return text


def load_auto(config: Path) -> AutoDownloader | None:
    """AutoDownloader from config.json (streamers from the roster), or None if
    `download.auto.enabled` is false."""
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
        retry_interval=float(auto.get("retry_minutes", 15)) * 60,
        give_up=float(auto.get("give_up_hours", 12)) * 3600,
        max_errors=int(auto.get("max_errors", 3)),
        default_keep=default_keep,
        transcribe=transcriber,
        analyze=analyzer,
        translate=load_translator(config, cfg) if transcriber else None,
        upload=load_uploader(config) if analyzer else None,  # uploads the analysis
    )
    ad.uploads = roster.upload_map()
    ad.site_url = os.getenv("SITE_URL", "").strip().rstrip("/")
    return ad


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
                            sheet_title=sheet_title(data, archive_date(table)))

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
    A broken LLM setup turns analysis off (logged) instead of stopping the service."""
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

    def run(video: Path, streamer: str, title: str) -> Path:
        return analyze(video, complete, streamer=streamer, title=title, settings=settings)

    return run


def attach(config: Path, mon: StreamMonitor) -> tuple[LiveNotifier | None, AutoDownloader | None]:
    """Hook notifications and auto-download onto a monitor. Run `auto.run()` as a task."""
    auto = load_auto(config)
    notifier = load_notifier(config, allow_empty=auto is not None)
    if notifier:
        mon.on_change.append(notifier.on_change)
    if auto and notifier:
        auto.notify = notifier.send
        notifier.on_ended.append(auto.on_ended)
    return notifier, auto
