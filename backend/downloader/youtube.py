"""YouTube: video and chat replay, both through yt-dlp.

Chat replay is yt-dlp's `live_chat` "subtitle": the raw YouTube chat actions, one JSON
object per line. YouTube only offers it for finished streams (and premieres), and
sometimes only hours after the stream ends.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.archive import Artifact, artifact_name

from .base import ChatNotReady, DownloadError, PlatformDownloader, VideoInfo, outtmpl

class YoutubeDownloader(PlatformDownloader):
    platform = "youtube"
    url_re = re.compile(r"(?:^|[/.])(?:youtube\.com|youtu\.be)/", re.I)

    def video_format(self) -> str:
        # Prefer H.264 + AAC in mp4 (what editors handle best), up to max_height.
        h = f"[height<={self.settings.max_height}]" if self.settings.max_height else ""
        return f"bv*{h}[ext=mp4]+ba[ext=m4a]/b{h}[ext=mp4]/bv*{h}+ba/b{h}/bv*+ba/b"

    def download_raw_chat(self, info: VideoInfo, out_dir: Path) -> Path:
        path = out_dir / artifact_name(info.stem, Artifact.RAW_CHAT, self.platform)
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
