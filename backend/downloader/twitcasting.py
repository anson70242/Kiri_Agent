"""TwitCasting: the replay of a finished live (a TwitCasting movie), with the owner's login.

Every replay on the site needs a logged-in account, so every request for a replay carries
the cookies of `DownloadSettings.twitcasting_cookies_file`; the YouTube cookies and the
browser fallback are never used here. The steps, each its own piece:

- replay state (`read_replay` classifies the replay page, loaded with the cookies, by markers
  verified on the real site on 2026-11-13; `published_replay` turns every state but published
  into its outcome: ReplayUnpublished, or a DownloadError for a missing login or a live of a
  kind not supported);
- video (`download_video`): through yt-dlp with the cookies, as on the other platforms;
- chat (`download_raw_chat`): not supported yet.

An archive's title is the live's own title (the monitor's title rule) followed by its start
time as `HHmmss`, in the clock of the date folder, so two lives with the same title on one day
are two archives, and the same replay downloaded twice is the same archive.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

import httpx
from yt_dlp.cookies import YoutubeDLCookieJar

from backend.monitor.base import USER_AGENT, meta_content
from backend.monitor.twitcasting import SITE, channel_id, data_attr, live_title

from .base import DownloadError, PlatformDownloader, ReplayUnpublished, VideoInfo

log = logging.getLogger(__name__)

REPLAY_RE = re.compile(r"twitcasting\.tv/([^/?#]+)/movie/(\d+)", re.I)


def replay_url(channel: str, live_id: str) -> str:
    """The replay page of a live (a TwitCasting movie) of a channel."""
    return f"{SITE}/{channel}/movie/{live_id}"


class ReplayState(Enum):
    """What the replay page, loaded with the login cookies, shows. Read from markers, not
    from text: the page's language follows the account."""

    PUBLISHED = "published"  # the player has `data-movie-playlist` (seen only when logged in)
    LOGIN_MISSING = "login missing"  # `data-account-type="not_logged_in"`: no or expired login
    UNPUBLISHED = "unpublished"  # logged in, no playlist, the player's empty message instead
    PASSWORD = "password"  # a password (合言葉) form instead of the player
    UNKNOWN = "unknown"  # none of the above (membership-only pages were never seen)


@dataclass(frozen=True)
class Replay:
    """What a replay page says."""

    url: str
    state: ReplayState
    title: str | None  # the live's own title (the title rule), None without og:title
    started_at: datetime | None  # the live's start time
    message: str = ""  # the player's own message when there is no player ("This video is not published")


def parse_replay(url: str, page: str) -> Replay:
    og_title = meta_content(page, "og:title")
    m = re.search(r'<time\b[^>]*\bdata-toggle="true"[^>]*\bdatetime="([^"]+)"', page)
    started = None
    if m:
        try:
            started = parsedate_to_datetime(m.group(1))
        except (TypeError, ValueError):
            started = None
    m = re.search(r'class="tw-player-empty-message"[^>]*>(.*?)</div>', page, re.S)
    message = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split()) if m else ""
    account = data_attr(page, "account-type")
    if re.search(r"\bdata-movie-playlist=", page):
        state = ReplayState.PUBLISHED
    elif re.search(r'<input\b[^>]*\bname="password"', page):
        state = ReplayState.PASSWORD
    elif account == "not_logged_in":
        state = ReplayState.LOGIN_MISSING
    elif account is not None and m:
        state = ReplayState.UNPUBLISHED
    else:
        state = ReplayState.UNKNOWN
    return Replay(url, state, live_title(og_title) if og_title else None, started, message)


class TwitCastingDownloader(PlatformDownloader):
    platform = "twitcasting"
    url_re = re.compile(r"(?:^|[/.])twitcasting\.tv/", re.I)
    # The HTTP transport of the replay page requests; tests put a fake site here.
    transport: ClassVar[httpx.BaseTransport | None] = None

    def find_replay(
        self, live_url: str, channel: str, live_id: str | None, started_at: datetime | None
    ) -> str | None:
        """The replay page of the ended live: the channel id and the live's (movie) id."""
        if not live_id:
            raise DownloadError("the TwitCasting live has no live id (movie id), so its replay cannot be found")
        return replay_url(channel_id(channel), live_id)

    # ---- replay state ----

    def published_replay(self, url: str) -> Replay:
        """The replay page, when the replay can be downloaded. Raises ReplayUnpublished when
        it is not published, DownloadError when the login is missing or expired (the text
        names the login and the cookies), and DownloadError saying the kind of live is not
        supported for a password-protected or unrecognised page."""
        replay = self.read_replay(url)
        if replay.state is ReplayState.PUBLISHED:
            return replay
        if replay.state is ReplayState.UNPUBLISHED:
            raise ReplayUnpublished(f"TwitCasting replay is not published: {url}")
        if replay.state is ReplayState.LOGIN_MISSING:
            raise DownloadError(f"TwitCasting login missing or expired: the replay page {url} "
                                f"says not logged in; replace the cookies file {self.cookies_file().name}")
        if replay.state is ReplayState.PASSWORD:
            raise DownloadError(f"TwitCasting replay {url} is password-protected (合言葉); "
                                "this kind of live is not supported")
        raise DownloadError(f"unrecognised TwitCasting replay page {url} "
                            f"({replay.message or 'no message on the page'}); password-protected "
                            "and members-only lives are not supported")

    def read_replay(self, url: str) -> Replay:
        """Load the replay page with the login cookies and classify it."""
        with self.http_client() as client:
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise DownloadError(f"TwitCasting replay page {url}: {e}") from e
            return parse_replay(url, resp.text)

    def http_client(self) -> httpx.Client:
        """A client carrying the TwitCasting login cookies."""
        f = self.cookies_file()
        jar = YoutubeDLCookieJar(str(f))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, ValueError) as e:  # http.cookiejar.LoadError is an OSError
            raise DownloadError(f"TwitCasting login missing: cannot read the cookies file {f}: {e}") from e
        return httpx.Client(
            transport=self.transport, cookies=jar, timeout=30, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )

    def cookies_file(self) -> Path:
        """The login cookies; raises the "login missing" failure when there are none."""
        f = self.settings.twitcasting_cookies_file
        if not (f and f.is_file()):
            raise DownloadError(f"TwitCasting login missing: no cookies file at {f or 'none configured'}")
        return f

    # ---- metadata ----

    def info(self, url: str) -> VideoInfo:
        """The replay's metadata. The title is the archive's: the live's own title and its
        start time as `HHmmss`, in the clock of the date folder."""
        m = REPLAY_RE.search(url)
        if not m:
            raise DownloadError(f"{url} is not a TwitCasting replay; give a replay address "
                                "(twitcasting.tv/<channel id>/movie/<movie id>)")
        replay = self.published_replay(url)
        raw = self._ytdlp(url, {"skip_download": True}, download=False)
        ts = replay.started_at.timestamp() if replay.started_at else raw.get("timestamp")
        title = replay.title or live_title(str(raw.get("title") or "No Title"))
        if ts:
            title = f"{title} {datetime.fromtimestamp(ts).strftime('%H%M%S')}"
        return self.parse_info(raw | {"title": title, "timestamp": ts, "release_timestamp": None,
                                      "uploader_id": m.group(1)}, url)

    def match_creator(self, raw: dict[str, Any]) -> str | None:
        """The roster entry whose channel is the replay's channel, however it is spelled."""
        channel = channel_id(str(raw.get("uploader_id") or "")).lower()
        return next((s.name for s in self.streamers if channel_id(s.channel).lower() == channel), None)

    # ---- video ----

    def base_options(self) -> dict[str, Any]:
        # yt-dlp writes refreshed cookies back to this file, keeping the login valid longer.
        return super().base_options() | {"cookiefile": str(self.cookies_file())}

    def cookie_source(self) -> tuple[str, dict[str, Any]] | None:
        return None  # the TwitCasting cookies are on every request, never a retry

    # ---- chat ----

    def download_raw_chat(self, info: VideoInfo, out_dir: Path) -> Path:
        raise DownloadError("TwitCasting chat is not supported yet")
