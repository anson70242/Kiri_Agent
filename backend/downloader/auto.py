"""Download a stream's archive automatically once it ends.

    LiveNotifier ("stream ended") -> AutoDownloader.on_ended (queue a job)
    -> AutoDownloader.run (one job at a time, in a thread) -> Discord via LiveNotifier.send

config.json:

    "monitor": { "streamers": [
      { "name": "...", "platform": "youtube", "channel": "@...", "auto_download": true, "keep": 6 }
    ]},
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
import functools
import json
import logging
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from backend.asr import ASRSettings, transcribe
from backend.monitor import StreamMonitor, Streamer, StreamStatus
from backend.notify import EventKind, LiveNotifier, StreamEvent, load_notifier

from . import REPO_ROOT, download, downloader_for, load_settings
from .base import DownloadError, DownloadResult, DownloadSettings
from .pending import PendingChats

log = logging.getLogger(__name__)

NOT_READY = ("is_live", "is_upcoming", "post_live")
AUTH_HINTS = ("member", "sign in", "cookie", "login", "subscriber", "private")


@dataclass
class Job:
    status: StreamStatus  # the stream's last live status (url, title, start time)
    queued_at: float
    next_try: float
    attempts: int = 0
    errors: int = 0  # failures other than "not ready yet"
    last_error: str = ""
    stage: Literal["download", "transcribe"] = "download"
    video: str | None = None  # set once downloaded

    @property
    def key(self) -> str:
        return f"{self.status.streamer.id}|{self.status.stream_id or self.status.url}"

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
        notify: Callable[[StreamEvent], None] | None = None,
        clock: Callable[[], float] = time.time,
        transcribe: Callable[[Path], Any] | None = None,  # video -> transcript; None: skip
    ):
        self.transcribe = transcribe
        self.settings = settings
        self.streamers = list(streamers)
        self.keep = keep
        self.state_file = state_file
        self.retry_interval = retry_interval
        self.give_up = give_up
        self.max_errors = max_errors
        self.notify = notify
        self.clock = clock
        self._wake = asyncio.Event()
        self._active: dict[str, Any] | None = None  # job being downloaded + progress

    # ---- queue ----

    def load(self) -> list[Job]:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.error("can't read %s (%s); treating the queue as empty", self.state_file, e)
            return []
        return [Job.from_dict(d) for d in raw]

    def save(self, jobs: Iterable[Job]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps([j.to_dict() for j in jobs], ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def on_ended(self, event: StreamEvent) -> None:
        """LiveNotifier.on_ended listener: queue the archive of a stream that ended."""
        status = event.status
        if status.streamer.id not in self.keep or not status.url:
            return
        jobs = self.load()
        now = self.clock()
        job = Job(status, queued_at=now, next_try=now)
        if any(j.key == job.key for j in jobs):
            return
        jobs.append(job)
        self.save(jobs)
        log.info("auto-download queued: %s %s", status.streamer.name, status.url)
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        """What's downloading now and what's queued, for the archives page."""
        return {
            "active": dict(a) if (a := self._active) else None,  # copy: the thread mutates it
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
        """Process due jobs forever (one at a time), plus the late-chat retry queue."""
        while True:
            self._wake.clear()
            try:
                await self.run_due()
                await self.retry_chats()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("auto-download worker error")
            try:
                await asyncio.wait_for(self._wake.wait(), self.sleep_time())
            except TimeoutError:
                pass

    def sleep_time(self) -> float:
        times = [j.next_try for j in self.load()]
        if queue := PendingChats.from_settings(self.settings):
            if (due := queue.next_due()) is not None:
                times.append(due)
        if not times:
            return 300
        return min(300.0, max(1.0, min(times) - self.clock()))

    async def run_due(self) -> None:
        while job := self.next_due():
            outcome = await asyncio.to_thread(self.attempt, job)
            self.finish(job, outcome)

    def next_due(self) -> Job | None:
        now = self.clock()
        due = [j for j in self.load() if j.next_try <= now]
        return min(due, key=lambda j: j.next_try) if due else None

    def attempt(self, job: Job) -> Outcome:
        """One try at the job's current stage (blocking)."""
        if job.stage == "transcribe":
            return self.attempt_transcribe(job)
        s = job.status
        try:
            dl = downloader_for(s.url or "", self.settings, self.streamers)
            url = dl.find_vod(s.url or "", s.streamer.channel, s.started_at)
            if not url:
                return Outcome("wait", "archive not listed yet")
            info = dl.info(url)
            if info.live_status in NOT_READY:
                return Outcome("wait", f"stream is {info.live_status}")
            self._active = {"name": s.streamer.name, "platform": s.streamer.platform,
                            "title": s.title, "url": s.url, "started": self.clock(), "stage": "download",
                            "percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}
            result = download(url, self.settings, self.streamers, info=info, progress=self._on_progress)
        except DownloadError as e:
            return Outcome("error", str(e))
        finally:
            self._active = None
        if err := result.errors.get("video"):
            return Outcome("error", err, result)
        pruned = self.prune(result.output_dir, self.keep.get(s.streamer.id, 6))
        return Outcome("done", result=result, pruned=pruned)

    def attempt_transcribe(self, job: Job) -> Outcome:
        s = job.status
        self._active = {"name": s.streamer.name, "platform": s.streamer.platform, "title": s.title,
                        "url": s.url, "started": self.clock(), "stage": "transcribe", "percent": None,
                        "downloaded": 0, "total": None, "speed": None, "eta": None}
        try:
            assert self.transcribe and job.video
            self.transcribe(Path(job.video))
        except Exception as e:  # noqa: BLE001 - ASR, ffmpeg or a missing file: all retried
            return Outcome("error", str(e) or type(e).__name__)
        finally:
            self._active = None
        return Outcome("done")

    def _on_progress(self, d: dict[str, Any]) -> None:
        """yt-dlp progress hook (runs in the download thread)."""
        active = self._active
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
        """Record an attempt's outcome; send a message when the job is over."""
        jobs = self.load()  # reload: on_ended may have added jobs meanwhile
        current = next((j for j in jobs if j.key == job.key), job)
        rest = [j for j in jobs if j.key != job.key]
        now = self.clock()
        current.attempts += 1
        current.last_error = outcome.message

        name = job.status.streamer.name
        if outcome.kind == "done" and current.stage == "transcribe":
            log.info("transcribed: %s %s", name, current.video)
            self.save(rest)
            return
        if outcome.kind == "done":
            log.info("auto-download done: %s %s", name, outcome.result and outcome.result.video)
            self._send(EventKind.DOWNLOADED, job, success_detail(outcome))
            video = outcome.result.video if outcome.result else None
            if self.transcribe and video:
                # Next stage: same job, fresh retry counters.
                current.stage, current.video = "transcribe", str(video)
                current.attempts, current.errors, current.last_error = 0, 0, ""
                current.next_try = now
                self.save(rest + [current])
                self._wake.set()
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
                else:
                    self._send(EventKind.DOWNLOAD_FAILED, job, failure_detail(current))
                return
        current.next_try = now + self.retry_interval
        self.save(rest + [current])

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
        """Delete the oldest `<creator>/<date>/<title>` folders beyond `keep`, never
        `protect` (the one just downloaded). Returns what was deleted."""
        root = self.settings.output_dir.resolve()
        creator = protect.parent.parent
        if creator.resolve().parent != root:  # never delete outside output_dir
            log.error("not pruning %s: not directly under %s", creator, root)
            return []
        folders = [t for d in creator.iterdir() if d.is_dir() for t in d.iterdir() if t.is_dir()]
        folders.sort(key=lambda p: (p.parent.name, p.stat().st_mtime))
        excess = len(folders) - keep
        removed: list[Path] = []
        queue = PendingChats.from_settings(self.settings)
        for folder in folders:
            if excess <= 0:
                break
            if folder == protect:
                continue
            shutil.rmtree(folder)
            removed.append(folder)
            excess -= 1
            log.info("pruned old archive: %s", folder)
            if queue:
                queue.drop_under(folder)
            if not any(folder.parent.iterdir()):
                folder.parent.rmdir()
        return removed


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


def failure_detail(job: Job) -> str:
    text = f"试了 {job.attempts} 次仍失败：\n```{job.last_error[:1500]}```"
    if any(h in job.last_error.lower() for h in AUTH_HINTS):
        if job.status.streamer.platform == "twitch":
            text += "\n可能需要更新服务器 `.env` 里的 `TWITCH_OAUTH`。"
        else:
            text += "\n可能是 cookies 过期：更新服务器上的 `data/youtube_cookies.txt`（见 docs/deploy.md）。"
    return text


def load_auto(config: Path) -> AutoDownloader | None:
    """AutoDownloader from config.json, or None if no streamer has `auto_download`."""
    cfg = json.loads(config.read_text(encoding="utf-8"))
    auto = (cfg.get("download") or {}).get("auto") or {}
    if auto.get("enabled") is False:
        return None
    default_keep = int(auto.get("keep", 6))
    keep = {
        Streamer(s.get("name") or s["channel"], s["platform"], s["channel"]).id: int(s.get("keep", default_keep))
        for s in (cfg.get("monitor") or {}).get("streamers", [])
        if s.get("auto_download")
    }
    if not keep:
        return None
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
    return AutoDownloader(
        settings,
        streamers,
        keep,
        state if state.is_absolute() else REPO_ROOT / state,
        retry_interval=float(auto.get("retry_minutes", 15)) * 60,
        give_up=float(auto.get("give_up_hours", 12)) * 3600,
        max_errors=int(auto.get("max_errors", 3)),
        transcribe=transcriber,
    )


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
