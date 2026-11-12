"""Shared pieces for every platform downloader.

A platform downloader subclasses `PlatformDownloader`, sets `platform` and `url_re`,
and implements `download_chat()`. Video download and metadata go through the yt-dlp
Python package for every platform; only chat differs per platform. Subclasses
register themselves, so `downloader_for(url)` picks the right one from the URL.
"""

from __future__ import annotations

import http.cookiejar
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote

import yt_dlp
from yt_dlp.utils import DownloadError as YtdlpError

from backend.archive import Artifact, artifact_name, name_archive
from backend.monitor import Streamer

from .chat_clean import clean_chat

log = logging.getLogger(__name__)


class DownloadError(Exception):
    """A video, chat or metadata download failed."""


class ChatNotReady(DownloadError):
    """Chat replay isn't available yet but may appear later (YouTube processes it for
    hours after a stream ends). `download()` queues these for retry."""


@dataclass
class DownloadSettings:
    output_dir: Path = Path("downloads")
    # When a plain attempt fails (members-only, age-restricted), retry once with
    # cookies: from this Netscape cookies.txt if it exists (servers have no browser),
    # else from this browser. Both None/missing: no retry.
    cookies_file: Path | None = None
    cookies_from_browser: str | None = None
    # Twitch `auth-token` cookie value, for subscriber-only VODs and their chat.
    twitch_oauth: str | None = None
    # ffmpeg binary or its folder. None means "find it on PATH".
    ffmpeg: str | None = None
    concurrent_fragments: int = 5
    max_height: int | None = 1080  # best video up to this height; None: no limit
    # Chats that weren't ready yet are queued here and retried. None: no queue.
    pending_file: Path | None = None
    chat_retry_interval: float = 30 * 60  # seconds between retries
    chat_retry_give_up: float = 48 * 3600  # drop an entry this long after it was queued


@dataclass(frozen=True)
class VideoInfo:
    platform: str
    id: str  # platform video id (Twitch: digits only, no "v" prefix)
    url: str
    title: str
    channel: str  # channel login / handle / id, used to match config streamers
    channel_name: str  # display name
    date: str  # YYYYMMDD, local time of the stream start
    duration: float | None = None
    creator: str = ""  # config streamer name, or channel_name when not in config
    live_status: str | None = None  # yt-dlp's: not_live / was_live / is_live / ...
    extractor: str = ""  # yt-dlp extractor_key, e.g. Youtube, TwitchVod, TwitchClips

    def __post_init__(self) -> None:
        if not self.creator:
            object.__setattr__(self, "creator", self.channel_name)

    @property
    def stem(self) -> str:
        """File name without extension: `[20261105][柊優花] title` (named by the Archive module)."""
        return name_archive(self.creator, self.date, self.title)[1]

    @property
    def folder(self) -> Path:
        """Relative output folder: `<creator>/<date>/<title>` (named by the Archive module)."""
        return name_archive(self.creator, self.date, self.title)[0]


@dataclass
class DownloadResult:
    info: VideoInfo
    output_dir: Path
    video: Path | None = None
    chat: Path | None = None
    errors: dict[str, str] = field(default_factory=dict)  # "video" / "chat" -> message
    chat_queued: bool = False  # chat wasn't ready; it's in the retry queue

    @property
    def ok(self) -> bool:
        return not self.errors


class PlatformDownloader(ABC):
    """Base class for one platform."""

    platform: ClassVar[str]
    url_re: ClassVar[re.Pattern[str]]
    registry: ClassVar[dict[str, type[PlatformDownloader]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "platform", None):
            PlatformDownloader.registry[cls.platform] = cls

    def __init__(self, settings: DownloadSettings, streamers: Iterable[Streamer] = ()):
        self.settings = settings
        self.streamers = [s for s in streamers if s.platform == self.platform]

    @classmethod
    def handles(cls, url: str) -> bool:
        return bool(cls.url_re.search(url))

    # ---- public API ----

    def info(self, url: str) -> VideoInfo:
        """Fetch metadata without downloading anything."""
        raw = self._ytdlp(url, {"skip_download": True}, download=False)
        return self.parse_info(raw, url)

    def download_video(
        self, info: VideoInfo, out_dir: Path, progress: Callable[[dict[str, Any]], None] | None = None
    ) -> Path:
        path = out_dir / artifact_name(info.stem, Artifact.VIDEO)
        if path.exists():
            log.info("video already exists, skipping: %s", path.name)
            return path
        log.info("downloading video: %s", info.url)
        opts = self.video_options(outtmpl(out_dir, info.stem))
        if progress:
            opts["progress_hooks"] = [progress]
        self._ytdlp(info.url, opts)
        if not path.exists():
            raise DownloadError(f"yt-dlp finished but {path.name} is missing")
        return path

    def download_chat(self, info: VideoInfo, out_dir: Path) -> Path:
        """Download the chat replay, clean it, and return the cleaned chat's file.
        The raw file stays next to it."""
        raw = self.download_raw_chat(info, out_dir)
        try:
            return clean_chat(raw)
        except (OSError, ValueError) as e:
            raise DownloadError(f"chat downloaded but cleaning failed ({raw.name}): {e}") from e

    @abstractmethod
    def download_raw_chat(self, info: VideoInfo, out_dir: Path) -> Path:
        """Save the platform's raw chat replay next to the video and return its path."""

    # ---- hooks for subclasses ----

    def video_options(self, outtmpl: Path) -> dict[str, Any]:
        """yt-dlp options for the video download; the result must end up as .mp4."""
        return {
            "outtmpl": str(outtmpl),
            "format": self.video_format(),
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": self.settings.concurrent_fragments,
        }

    def video_format(self) -> str:
        """yt-dlp format: best up to `max_height`, falling back to plain best."""
        h = f"[height<={self.settings.max_height}]" if self.settings.max_height else ""
        return f"bv*{h}+ba/b{h}/bv*+ba/b"

    def add_cookies(self, ydl: yt_dlp.YoutubeDL) -> None:
        """Put platform auth cookies into a fresh YoutubeDL."""

    def find_vod(self, stream_url: str, channel: str, started_at: datetime | None) -> str | None:
        """URL of the archive of a stream that just ended (from the monitor), or None
        if it isn't up yet. YouTube: the stream URL itself is the archive."""
        return stream_url

    # ---- metadata ----

    def parse_info(self, raw: dict[str, Any], url: str) -> VideoInfo:
        channel = str(raw.get("uploader_id") or raw.get("channel_id") or raw.get("uploader") or "")
        channel_name = str(raw.get("channel") or raw.get("uploader") or channel or "Unknown")
        ts = raw.get("release_timestamp") or raw.get("timestamp")
        date = datetime.fromtimestamp(ts).strftime("%Y%m%d") if ts else raw.get("upload_date") or "19700101"
        return VideoInfo(
            platform=self.platform,
            id=self.video_id(raw),
            url=raw.get("webpage_url") or url,
            title=raw.get("title") or "No Title",
            channel=channel,
            channel_name=channel_name,
            date=date,
            duration=raw.get("duration"),
            creator=self.match_creator(raw) or channel_name,
            live_status=raw.get("live_status"),
            extractor=raw.get("extractor_key") or "",
        )

    def video_id(self, raw: dict[str, Any]) -> str:
        return str(raw.get("id") or "")

    def match_creator(self, raw: dict[str, Any]) -> str | None:
        """Name of the config streamer that owns this video, if any."""
        ids = {
            norm(str(v))
            for k in ("uploader_id", "channel_id", "uploader", "channel")
            if (v := raw.get(k))
        }
        for s in self.streamers:
            if norm(s.channel) in ids:
                return s.name
        return None

    # ---- yt-dlp ----

    def base_options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "logger": YtdlpLogger(),
        }
        if self.settings.ffmpeg:
            opts["ffmpeg_location"] = self.settings.ffmpeg
        if deno := find_deno():
            opts["js_runtimes"] = {"deno": {"path": deno}}
        return opts

    def _ytdlp(self, url: str, extra: dict[str, Any], download: bool = True) -> dict[str, Any]:
        """Run yt-dlp. On failure, retry once with cookies if configured."""
        opts = self.base_options() | extra
        try:
            return self._run_ytdlp(url, opts, download)
        except YtdlpError as e:
            first = clean_error(e)
            source = self.cookie_source()
            if not source:
                raise DownloadError(first) from e
            label, cookie_opts = source
            log.warning("%s; retrying with %s", first, label)
            try:
                return self._run_ytdlp(url, opts | cookie_opts, download)
            except YtdlpError as e2:
                raise DownloadError(f"{first} | with {label}: {clean_error(e2)}") from e2

    def cookie_source(self) -> tuple[str, dict[str, Any]] | None:
        """yt-dlp options for the cookie retry: cookies file first, then browser."""
        f = self.settings.cookies_file
        if f and f.is_file():
            # yt-dlp writes refreshed cookies back to this file, keeping it valid longer.
            return f"cookies file {f.name}", {"cookiefile": str(f)}
        if browser := self.settings.cookies_from_browser:
            return f"{browser} cookies", {"cookiesfrombrowser": (browser,)}
        return None

    def _run_ytdlp(self, url: str, opts: dict[str, Any], download: bool) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            self.add_cookies(ydl)
            raw = ydl.extract_info(url, download=download)
            return ydl.sanitize_info(raw) or {}


class YtdlpLogger:
    """yt-dlp's chatter goes to debug; its errors too, since we raise them anyway."""

    def debug(self, msg: str) -> None:
        log.debug("%s", msg)

    info = debug

    def warning(self, msg: str) -> None:
        log.warning("%s", msg)

    def error(self, msg: str) -> None:
        log.debug("%s", msg)


def outtmpl(out_dir: Path, stem: str) -> Path:
    """yt-dlp output template for `<stem>.<ext>`; a literal % in a title must be %%."""
    return out_dir / (stem.replace("%", "%%") + ".%(ext)s")


def norm(channel: str) -> str:
    """Comparable form of a channel id: decoded, no leading @, lowercase."""
    return unquote(channel).strip().lstrip("@").lower()


def clean_error(e: Exception) -> str:
    """First line of a yt-dlp error, without its generic "how to pass cookies" advice."""
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip().removeprefix("ERROR:").strip()
    if not msg:
        return type(e).__name__
    return msg.splitlines()[0].split(" Use --cookies", 1)[0].strip()


def make_cookie(name: str, value: str, domain: str) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
        path="/", path_specified=True, secure=True, expires=None, discard=True,
        comment=None, comment_url=None, rest={},
    )


def find_deno() -> str | None:
    """deno from the `deno` PyPI package (yt-dlp[deno]); YouTube needs a JS runtime."""
    try:
        import deno

        return deno.find_deno_bin()
    except Exception:  # noqa: BLE001 - not installed: yt-dlp falls back to PATH
        return None
