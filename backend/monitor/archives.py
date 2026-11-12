"""Archives (录播) and 路灯表 pages for the dashboard: browse the archives, their chat,
subtitles and highlight tables.

The layout on disk and the folder references clients send belong to backend.archive; this
module turns archives into JSON, SRT, VTT, zip and Excel downloads, and applies who may see
which creator on top.
"""

import shutil
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from backend.archive import Archive, ArchiveNotFound, ArtifactDamaged, ArtifactMissing, Artifact, Downloads
from backend.sheet import highlights_xlsx

from .__main__ import REPO_ROOT

PAGE = REPO_ROOT / "frontend" / "archives.html"
HIGHLIGHTS_PAGE = REPO_ROOT / "frontend" / "highlights.html"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _seconds(t: str) -> int:
    """"HH:MM:SS" / "MM:SS" -> seconds; bad input -> 0."""
    try:
        total = 0
        for part in t.split(":"):
            total = total * 60 + int(part)
        return total
    except (ValueError, AttributeError):
        return 0


def summary(a: Archive) -> dict:
    """One archive as the 录播 page lists it."""

    def name(artifact: Artifact) -> str | None:
        f = a.file(artifact)
        return f.name if f else None

    video = a.video
    return {
        "creator": a.creator,
        "date": a.date_folder,
        "title": a.title,
        "folder": a.ref,
        "video": video.name if video else None,
        "video_size": a.video_size,
        "downloading": a.downloading,
        "has_chat": a.has(Artifact.CHAT),
        "chat_file": name(Artifact.CHAT),
        "raw_chat_file": name(Artifact.RAW_CHAT),
        "chat_count": a.chat_count(),
        "transcript_file": name(Artifact.TRANSCRIPT),
        "zh_transcript_file": name(Artifact.CHINESE_TRANSCRIPT),
        "highlights_file": name(Artifact.HIGHLIGHTS),
        "size": a.size,
        "mtime": a.mtime,
    }


def srt(segments: list[dict]) -> str:
    def t(s: float) -> str:
        return _vtt_time(s).replace(".", ",")
    return "".join(f"{i}\n{t(s['start'])} --> {t(s['end'])}\n{s['text']}\n\n" for i, s in enumerate(segments, 1))


def _vtt_time(seconds: float) -> str:
    ms = round(max(0.0, float(seconds)) * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def attachment(filename: str) -> str:
    """Content-Disposition for a download with a non-ASCII (Japanese) file name."""
    return f"attachment; filename*=UTF-8''{quote(filename)}"


class _Sink:
    """Write-only, unseekable buffer: zipfile writes into it, the generator drains it."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0

    def write(self, b: bytes) -> int:
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        data = bytes(self.buf)
        self.buf.clear()
        return data


def stream_zip(files: list[Path], extra: dict[str, bytes]) -> Iterator[bytes]:
    """A zip (stored, ZIP64) of `files` plus in-memory `extra` files, produced chunk by
    chunk, so a multi-GB video never needs a temp file or memory of its size."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:  # type: ignore[arg-type]
        for f in files:
            with f.open("rb") as src, zf.open(f.name, "w", force_zip64=True) as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                    if len(sink.buf) >= 1 << 20:
                        yield sink.drain()
            yield sink.drain()
        for name, data in extra.items():
            zf.writestr(name, data)
    yield sink.drain()  # central directory


def create_router(output_dir: Path,
                  visible: Callable[[Request], set[str] | None] = lambda request: None) -> APIRouter:
    """`visible(request)`: the streamer (creator folder) names that request may see, or
    None for everyone (no login, admins, members allowed "all")."""
    router = APIRouter()
    downloads = Downloads(output_dir)
    root = downloads.root

    def may_see(request: Request, creator: str) -> bool:
        names = visible(request)
        return names is None or creator in names

    def open_archive(folder: str, request: Request) -> Archive:
        try:
            archive = downloads.open(folder)
        except ArchiveNotFound:
            raise HTTPException(404, "archive not found")
        if not may_see(request, archive.creator):
            raise HTTPException(404, "archive not found")  # same answer as "doesn't exist"
        return archive

    def seen_archives(request: Request) -> list[Archive]:
        return [a for a in downloads.list() if may_see(request, a.creator)]

    @router.get("/archives", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(PAGE)

    @router.get("/api/archives")
    def archives(request: Request) -> dict:
        disk = None
        try:
            u = shutil.disk_usage(root if root.exists() else root.parent)
            disk = {"total": u.total, "used": u.used, "free": u.free}
        except OSError:
            pass
        return {"output_dir": str(root), "disk": disk, "archives": [summary(a) for a in seen_archives(request)]}

    @router.get("/api/archives/chat")
    def chat(folder: str, request: Request) -> dict:
        try:
            messages = open_archive(folder, request).read_chat()
        except ArtifactMissing:
            raise HTTPException(404, "no parsed chat")
        except ArtifactDamaged:
            raise HTTPException(500, "chat file is not valid JSON")
        density: list[int] = []
        for m in messages:
            minute = _seconds(m.get("time", "")) // 60
            if minute >= len(density):
                density.extend([0] * (minute + 1 - len(density)))
            density[minute] += 1
        return {"messages": messages, "density": density}

    def load_transcript(folder: str, lang: str, request: Request) -> list[dict]:
        try:
            return open_archive(folder, request).read_transcript(lang)
        except ArtifactMissing:
            raise HTTPException(404, "no transcript")
        except ArtifactDamaged:
            raise HTTPException(500, "transcript is not valid JSON")

    # ---- 路灯表 ----

    def load_highlights(archive: Archive) -> dict:
        try:
            return archive.read_highlights()
        except ArtifactMissing:
            raise HTTPException(404, "no 路灯表 for this archive")
        except ArtifactDamaged:
            raise HTTPException(500, "路灯表 is not valid JSON")

    @router.get("/highlights", include_in_schema=False)
    def highlights_page() -> FileResponse:
        return FileResponse(HIGHLIGHTS_PAGE)

    @router.get("/api/highlights")
    def highlights_list(request: Request) -> dict:
        """Every archive that has a 路灯表, newest first, with a one-line summary."""
        tables = []
        for a in seen_archives(request):
            try:
                rows = a.read_highlights().get("rows") or []
            except (ArtifactMissing, ArtifactDamaged, OSError):
                continue
            scores = [r["score"] for r in rows if isinstance(r.get("score"), int)]
            tables.append({
                "folder": a.ref, "creator": a.creator, "date": a.date_folder, "title": a.title,
                "segments": len(rows),
                "best_score": max(scores, default=None),
                "picks": sum(1 for r in rows if r.get("top_pick")),
            })
        return {"tables": tables}

    @router.get("/api/highlights/table")
    def highlights_table(folder: str, request: Request) -> dict:
        return load_highlights(open_archive(folder, request))

    @router.get("/api/highlights/xlsx")
    def highlights_xlsx_download(folder: str, request: Request) -> Response:
        """The 路灯表 as a formatted Excel sheet."""
        archive = open_archive(folder, request)
        table = load_highlights(archive)
        file = archive.file(Artifact.HIGHLIGHTS)
        assert file is not None  # it was just read
        name = Artifact.HIGHLIGHTS.stem_of(file.name) + ".highlights.xlsx"
        return Response(highlights_xlsx(table), media_type=XLSX_TYPE,
                        headers={"Content-Disposition": attachment(name)})

    @router.get("/api/archives/zip")
    def zip_download(folder: str, request: Request) -> StreamingResponse:
        """What editors use, zipped on the fly (no temp file): the video, the cleaned chat,
        and the Chinese and Japanese subtitles as SRT (made from the transcripts)."""
        archive = open_archive(folder, request)
        files = sorted(archive.files(Artifact.VIDEO) + archive.files(Artifact.CHAT))
        extra: dict[str, bytes] = {}
        for lang in ("zh", "ja"):
            artifact = Artifact.transcript(lang)
            if t := archive.file(artifact):
                try:
                    extra[f"{artifact.stem_of(t.name)}.{lang}.srt"] = srt(archive.read_transcript(lang)).encode()
                except (ArtifactMissing, ArtifactDamaged):
                    pass
        return StreamingResponse(stream_zip(files, extra), media_type="application/zip",
                                 headers={"Content-Disposition": attachment(archive.title + ".zip")})

    @router.get("/api/archives/transcript")
    def transcript(folder: str, request: Request, lang: str = "ja") -> dict:
        return {"segments": load_transcript(folder, lang, request)}

    @router.get("/api/archives/srt")
    def srt_download(folder: str, request: Request, lang: str = "ja") -> Response:
        """Subtitles as an .srt file (`lang` ja or zh)."""
        name = open_archive(folder, request).title + (".zh.srt" if lang == "zh" else ".ja.srt")
        return Response(srt(load_transcript(folder, lang, request)), media_type="application/x-subrip; charset=utf-8",
                        headers={"Content-Disposition": attachment(name)})

    @router.get("/api/archives/subtitles.vtt")
    def subtitles(folder: str, request: Request, lang: str = "ja") -> Response:
        """The transcript (`lang` ja or zh) as WebVTT, for <track> on the video."""
        cues = "".join(
            f"\n{_vtt_time(s['start'])} --> {_vtt_time(s['end'])}\n{s['text']}\n"
            for s in load_transcript(folder, lang, request)
        )
        return Response("WEBVTT\n" + cues, media_type="text/vtt; charset=utf-8")

    @router.get("/api/archives/file")
    def file(folder: str, name: str, request: Request, download: bool = False) -> FileResponse:
        try:
            path = open_archive(folder, request).member(name)
        except ValueError:
            raise HTTPException(400, "bad file name")
        if path is None:
            raise HTTPException(404, "file not found")
        # FileResponse handles Range requests, so <video> can seek.
        return FileResponse(path, filename=name, content_disposition_type="attachment" if download else "inline")

    return router
