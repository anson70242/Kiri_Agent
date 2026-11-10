"""YouTube: video and chat replay, both through yt-dlp.

Chat replay is yt-dlp's `live_chat` "subtitle": the raw YouTube chat actions, one JSON
object per line. YouTube only offers it for finished streams (and premieres), and
sometimes only hours after the stream ends.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import ChatNotReady, DownloadError, PlatformDownloader, VideoInfo, outtmpl

# Formats that stay H.264 + AAC in mp4 (what editors handle best), else anything.
VIDEO_FORMAT = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"


class YoutubeDownloader(PlatformDownloader):
    platform = "youtube"
    url_re = re.compile(r"(?:^|[/.])(?:youtube\.com|youtu\.be)/", re.I)

    def video_options(self, outtmpl: Path) -> dict[str, Any]:
        return super().video_options(outtmpl) | {"format": VIDEO_FORMAT}

    def download_chat(self, info: VideoInfo, out_dir: Path) -> Path:
        path = out_dir / f"{info.stem}.live_chat.json"
        if path.exists():
            return path
        try:
            self._ytdlp(info.url, {
                "skip_download": True,
                "writesubtitles": True,
                "subtitleslangs": ["live_chat"],
                "outtmpl": str(outtmpl(out_dir, info.stem)),
            })
        except DownloadError as e:
            if info.live_status == "post_live":  # still processing: worth retrying later
                raise ChatNotReady(f"stream still processing: {e}") from e
            raise
        if not path.exists():
            raise ChatNotReady("no chat replay yet (YouTube can take hours after the stream ends)")
        return path
