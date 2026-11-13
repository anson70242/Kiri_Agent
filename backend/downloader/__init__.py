"""VOD + chat replay downloader (YouTube, Twitch, TwitCasting). Importing a platform module registers it.

    from backend.downloader import download, load_settings
    settings, streamers = load_settings(Path("config.json"))
    result = download(url, settings, streamers)   # result.video, result.chat, result.errors

Blocking (yt-dlp is synchronous); from async code use `await asyncio.to_thread(download, ...)`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from backend.monitor import Streamer
from backend.monitor.roster import load_roster

from . import twitcasting, twitch, youtube  # noqa: F401  (registers each PlatformDownloader)
from .base import (
    ChatNotReady, DownloadError, DownloadResult, DownloadSettings, PlatformDownloader, ReplayUnpublished, VideoInfo)
from .pending import PendingChats

__all__ = [
    "ChatNotReady", "DownloadError", "DownloadResult", "DownloadSettings", "PendingChats",
    "PlatformDownloader", "ReplayUnpublished", "VideoInfo", "download", "downloader_for", "load_settings",
]

log = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_settings(config: Path) -> tuple[DownloadSettings, list[Streamer]]:
    """Read config.json's `download` section (+ .env secrets) and the monitor's streamers.

    "download": {
      "output_dir": "downloads",
      "cookies_file": "data/youtube_cookies.txt",
      "cookies_from_browser": "firefox",
      "twitcasting_cookies_file": "data/twitcasting_cookies.txt",
      "twitch_oauth_env": "TWITCH_OAUTH",
      "ffmpeg": "",
      "max_height": 1080,
      "chat_retry": { "interval_minutes": 30, "give_up_hours": 48 }
    }

    The streamers (the roster, see backend/monitor/roster.py) name the output folders: a video from a
    configured channel goes under that streamer's name.
    """
    load_dotenv(REPO_ROOT / ".env")
    cfg = json.loads(config.read_text(encoding="utf-8"))
    d = cfg.get("download") or {}
    out = Path(d.get("output_dir") or "downloads")
    retry = d.get("chat_retry") or {}
    pending = Path(retry.get("state_file") or "data/pending_chat.json")
    cookies = Path(d.get("cookies_file") or "data/youtube_cookies.txt")
    tc_cookies = Path(d.get("twitcasting_cookies_file") or "data/twitcasting_cookies.txt")
    settings = DownloadSettings(
        output_dir=out if out.is_absolute() else REPO_ROOT / out,
        cookies_file=cookies if cookies.is_absolute() else REPO_ROOT / cookies,
        twitcasting_cookies_file=tc_cookies if tc_cookies.is_absolute() else REPO_ROOT / tc_cookies,
        cookies_from_browser=d.get("cookies_from_browser") or None,
        # twitch_OAuth: AutoKiri's .env spelling (env names are case-sensitive on Linux).
        twitch_oauth=(os.getenv(d.get("twitch_oauth_env") or "TWITCH_OAUTH") or os.getenv("twitch_OAuth") or "").strip() or None,
        ffmpeg=d.get("ffmpeg") or None,
        concurrent_fragments=int(d.get("concurrent_fragments", 5)),
        max_height=int(h) if (h := d.get("max_height", 1080)) else None,
        pending_file=pending if pending.is_absolute() else REPO_ROOT / pending,
        chat_retry_interval=float(retry.get("interval_minutes", 30)) * 60,
        chat_retry_give_up=float(retry.get("give_up_hours", 48)) * 3600,
    )
    return settings, load_roster(config).streamers()


def downloader_for(
    url: str, settings: DownloadSettings, streamers: Iterable[Streamer] = ()
) -> PlatformDownloader:
    for cls in PlatformDownloader.registry.values():
        if cls.handles(url):
            return cls(settings, streamers)
    known = ", ".join(sorted(PlatformDownloader.registry))
    raise DownloadError(f"unsupported URL {url!r} (supported: {known})")


def download(
    url: str,
    settings: DownloadSettings,
    streamers: Iterable[Streamer] = (),
    *,
    video: bool = True,
    chat: bool = True,
    info: VideoInfo | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> DownloadResult:
    """Download a VOD and/or its chat into `<output_dir>/<creator>/<date>/<title>/`.

    Raises DownloadError only when the URL is unsupported, its metadata can't be
    read, or the stream is still live. A failed video or chat is recorded in
    `result.errors` and the other part still runs. A chat that isn't ready yet
    (ChatNotReady) goes into the retry queue (`settings.pending_file`).

    Right after a YouTube stream ends it is "post_live" (still processing): the video
    is refused for now, and the chat is tried once and queued if it isn't there yet.

    `info`: metadata already fetched with `dl.info(url)`, to skip fetching it again.
    `progress`: yt-dlp progress hook for the video (called from this thread).
    """
    dl = downloader_for(url, settings, streamers)
    info = info or dl.info(url)
    if info.live_status in ("is_live", "is_upcoming"):
        raise DownloadError(f"stream is {info.live_status}; download it after the VOD is ready")
    out_dir = settings.output_dir / info.folder
    out_dir.mkdir(parents=True, exist_ok=True)
    result = DownloadResult(info, out_dir)

    if video:
        try:
            if info.live_status == "post_live":
                raise DownloadError("YouTube is still processing the VOD; retry later")
            result.video = dl.download_video(info, out_dir, progress)
        except DownloadError as e:
            log.error("video download failed: %s", e)
            result.errors["video"] = str(e)
    if chat:
        try:
            result.chat = dl.download_chat(info, out_dir)
        except ChatNotReady as e:
            result.errors["chat"] = str(e)
            if queue := PendingChats.from_settings(settings):
                queue.add(info, out_dir, str(e))
                result.chat_queued = True
            else:
                log.error("chat download failed: %s", e)
        except DownloadError as e:
            log.error("chat download failed: %s", e)
            result.errors["chat"] = str(e)

    if not any(out_dir.iterdir()):
        out_dir.rmdir()  # nothing landed: don't leave an empty folder behind
    return result
