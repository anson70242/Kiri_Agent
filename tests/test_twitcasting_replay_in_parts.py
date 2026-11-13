"""A TwitCasting replay in several parts, through the download entry point, with the site and
yt-dlp faked (tests/twitcasting_fakes.py).

No real replay in several parts was found to test. The fake below answers the way yt-dlp's
TwitCasting extractor (`yt_dlp/extractor/twitcasting.py`) describes one: a `multi_video`
result whose entries `<movie>-0`, `<movie>-1`, ... are the parts, each carrying the whole
replay's metadata (its duration too). Downloading it behaves like yt-dlp, checked by hand
against yt-dlp and ffmpeg: a part the match filter refuses is not downloaded, a part already
on disk is not downloaded again, each part goes to the default output template with its
`playlist_index`, and the FFmpegConcat post-processor (a stream copy, never a re-encode) joins
the parts in order into the `pl_video` template and deletes them, or fails and leaves them.
"""

from __future__ import annotations

from pathlib import Path

from yt_dlp.utils import PostProcessingError

from backend.archive import Archive
from backend.downloader import download

from .twitcasting_fakes import MIKA, MOVIE, REPLAY, FakeYtdlp, replay_page, tc, ytdlp_raw  # noqa: F401 (tc: fixture)

PART_SECONDS = (1800.0, 1200.0, 600.0)


def in_parts(raw: dict, seconds: tuple[float, ...] = PART_SECONDS) -> dict:
    """What yt-dlp reports for a replay in parts: the top level's duration is the sum of the
    parts', and every entry repeats the top level's metadata."""
    whole = raw | {"duration": sum(seconds)}
    return whole | {"_type": "multi_video", "entries": [
        whole | {"id": f"{MOVIE}-{n}", "url": f"https://dl.example/{MOVIE}/{n}/master.m3u8", "ext": "mp4"}
        for n in range(len(seconds))]}


class FakeYtdlpInParts(FakeYtdlp):
    """FakeYtdlp that downloads a `multi_video` result part by part, as yt-dlp does."""

    join_fails = False

    def extract_info(self, url: str, download: bool = True) -> dict:
        if not download or self.raw.get("_type") != "multi_video":
            return super().extract_info(url, download)
        FakeYtdlp.calls.append((url, self.opts, download))
        templates = self.opts["outtmpl"] if isinstance(self.opts["outtmpl"], dict) else {"default": self.opts["outtmpl"]}
        keep = self.opts.get("match_filter") or (lambda info, incomplete: None)
        parts = []
        for index, entry in enumerate(self.raw["entries"], 1):
            info = entry | {"playlist_index": index}
            if keep(info, incomplete=True) is not None:
                continue
            part = Path(fill(templates["default"], info))
            if not part.exists():
                part.write_bytes(f"<part {index}>".encode())
            parts.append(part)
        joins = [pp for pp in self.opts.get("postprocessors", []) if pp.get("when") == "playlist"]
        assert all(pp["key"] == "FFmpegConcat" for pp in joins), "the fake knows only the stream-copy join"
        if joins and parts:
            if self.join_fails:
                raise PostProcessingError("The files have different streams/codecs and cannot be concatenated")
            joined = Path(fill(templates.get("pl_video", templates["default"]), self.raw | {"ext": "mp4"}))
            joined.write_bytes(b"".join(p.read_bytes() for p in parts))
            for p in parts:
                p.unlink()
        return dict(self.raw)


def fill(template: str, info: dict) -> str:
    """An output template's file name, for the fields the downloader uses."""
    for key in ("playlist_index", "ext"):
        if key in info:
            template = template.replace(f"%({key})s", str(info[key]))
    return template.replace("%%", "%")


def test_a_replay_in_several_parts_is_joined_in_order_into_one_video_named_after_the_archive(tc, monkeypatch):
    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYtdlpInParts)
    monkeypatch.setattr(FakeYtdlp, "raw", in_parts(ytdlp_raw()))

    r = download(REPLAY, tc.settings, [MIKA], chat=False)

    assert r.ok
    assert r.video == r.output_dir / "[20261006][Mika] Night chat 013044.mp4"
    assert r.video.read_bytes() == b"<part 1><part 2><part 3>"
    assert [p.name for p in r.output_dir.iterdir()] == [r.video.name]  # the parts are gone
    archive = Archive.from_folder(r.output_dir)
    assert archive.video == r.video and not archive.downloading
