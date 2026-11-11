"""Twitch: video through yt-dlp, chat replay straight from Twitch's GQL API.

Chat is paged by time: ask for the comments from offset N seconds, continue from the
last comment's offset, drop ids already seen. The output JSON follows the layout of
TwitchDownloaderCLI's chat JSON (`comments[].content_offset_seconds`,
`commenter.display_name`, `message.body`, ...), so tools written for it keep working.

Subscriber-only VODs need `TWITCH_OAUTH` (the `auth-token` cookie of a logged-in
twitch.tv session that can watch the VOD).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yt_dlp

from backend.monitor.base import USER_AGENT

from .base import DownloadError, PlatformDownloader, VideoInfo, make_cookie

log = logging.getLogger(__name__)

GQL_URL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch web player's public client id
COMMENTS_QUERY = (
    'query{video(id:"%s"){comments(contentOffsetSeconds:%d){edges{node{'
    "id,createdAt,contentOffsetSeconds,commenter{id,login,displayName},"
    "message{fragments{text,emote{emoteID}},userBadges{setID,version},userColor}"
    "}},pageInfo{hasNextPage}}}}"
)
RETRIES = 5
VOD_MATCH_SECONDS = 30 * 60  # archive start vs. the start time the monitor saw


class TwitchDownloader(PlatformDownloader):
    platform = "twitch"
    url_re = re.compile(r"(?:^|[/.])twitch\.tv/", re.I)

    def video_id(self, raw: dict[str, Any]) -> str:
        # yt-dlp reports VOD ids as "v123456".
        vid = str(raw.get("id") or "")
        return vid[1:] if re.fullmatch(r"v\d+", vid) else vid

    def add_cookies(self, ydl: yt_dlp.YoutubeDL) -> None:
        if self.settings.twitch_oauth:
            ydl.cookiejar.set_cookie(make_cookie("auth-token", self.settings.twitch_oauth, ".twitch.tv"))

    def find_vod(self, stream_url: str, channel: str, started_at: datetime | None) -> str | None:
        """The channel's newest archive, if it is the broadcast that started at
        `started_at` (within VOD_MATCH_SECONDS). None: not there (yet)."""
        login = channel.rstrip("/").rsplit("/", 1)[-1]
        listing = self._ytdlp(
            f"https://www.twitch.tv/{login}/videos?filter=archives&sort=time",
            {"extract_flat": True, "playlistend": 3},
            download=False,
        )
        for entry in listing.get("entries") or []:
            url = entry.get("url")
            if not url:
                continue
            if started_at is None:
                return url  # nothing to match against: trust the newest
            raw = self._ytdlp(url, {"skip_download": True}, download=False)
            ts = raw.get("timestamp")
            if ts and abs(ts - started_at.timestamp()) <= VOD_MATCH_SECONDS:
                return url
        return None

    def download_raw_chat(self, info: VideoInfo, out_dir: Path) -> Path:
        if info.extractor != "TwitchVod":
            raise DownloadError("chat replay is only available for VODs, not clips")
        path = out_dir / f"{info.stem}.chat.json"
        if path.exists():
            return path
        headers = {"Client-ID": CLIENT_ID, "User-Agent": USER_AGENT}
        if self.settings.twitch_oauth:
            headers["Authorization"] = f"OAuth {self.settings.twitch_oauth}"
        with httpx.Client(headers=headers, timeout=30) as client:
            comments = fetch_comments(client, info.id, info.duration)
        data = {
            "streamer": {"name": info.channel_name, "login": info.channel},
            "video": {
                "id": info.id,
                "title": info.title,
                "url": info.url,
                "length": info.duration,
            },
            "comments": comments,
        }
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path


def fetch_comments(client: httpx.Client, video_id: str, duration: float | None = None) -> list[dict[str, Any]]:
    """Every chat comment of a VOD, oldest first, in TwitchDownloader's layout."""
    comments: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    last_log = time.monotonic()
    while True:
        page = gql_comments(client, video_id, offset)
        edges = page.get("edges") or []
        new = [e["node"] for e in edges if e["node"]["id"] not in seen]
        for node in new:
            seen.add(node["id"])
            comments.append(convert_comment(node))
        if not edges or not (page.get("pageInfo") or {}).get("hasNextPage"):
            break
        last = int(edges[-1]["node"]["contentOffsetSeconds"])
        # A whole page within one second (or all already seen): step past it.
        offset = last if last > offset and new else offset + 1
        if time.monotonic() - last_log > 10:
            total = f"/{int(duration)}" if duration else ""
            log.info("chat: %d comments, at %ds%s", len(comments), offset, total)
            last_log = time.monotonic()
    comments.sort(key=lambda c: c["content_offset_seconds"])
    return comments


def gql_comments(client: httpx.Client, video_id: str, offset: int) -> dict[str, Any]:
    """One page of comments from `offset` seconds. Retries transient failures."""
    query = COMMENTS_QUERY % (video_id, offset)
    for attempt in range(RETRIES):
        try:
            resp = client.post(GQL_URL, json={"query": query})
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            if attempt == RETRIES - 1:
                raise DownloadError(f"Twitch chat request failed: {e}") from e
            time.sleep(2**attempt)
            continue
        video = (body.get("data") or {}).get("video")
        if body.get("errors") and not video:
            raise DownloadError(f"Twitch GQL error: {body['errors'][0].get('message')}")
        if video is None:
            raise DownloadError(f"VOD {video_id} not found (deleted, or subscriber-only without TWITCH_OAUTH)")
        comments = video.get("comments")
        if comments is None:
            raise DownloadError(f"VOD {video_id}: chat is not available")
        return comments
    raise AssertionError("unreachable")


def convert_comment(node: dict[str, Any]) -> dict[str, Any]:
    """GQL comment node -> TwitchDownloader chat JSON comment."""
    commenter = node.get("commenter") or {}
    message = node.get("message") or {}
    fragments = [
        {
            "text": f.get("text") or "",
            "emoticon": {"emoticon_id": f["emote"]["emoteID"]} if f.get("emote") else None,
        }
        for f in message.get("fragments") or []
    ]
    return {
        "_id": node["id"],
        "created_at": node.get("createdAt"),
        "content_offset_seconds": node.get("contentOffsetSeconds", 0),
        "commenter": {
            "_id": commenter.get("id"),
            "name": commenter.get("login"),
            "display_name": commenter.get("displayName") or commenter.get("login") or "Unknown",
        },
        "message": {
            "body": "".join(f["text"] for f in fragments),
            "fragments": fragments,
            "user_badges": [
                {"_id": b.get("setID"), "version": b.get("version")}
                for b in message.get("userBadges") or []
            ],
            "user_color": message.get("userColor"),
        },
    }
