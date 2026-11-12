"""Archives (录播) pages for the dashboard: browse downloaded VODs and their chat.

Downloads live in `<output_dir>/<creator>/<YYYYMMDD>/<title>/` (see
backend/downloader/base.py VideoInfo.folder). Every path a client sends is resolved
and must stay inside output_dir.
"""

import json
import shutil
import zipfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from backend.sheet import highlights_xlsx

from .__main__ import REPO_ROOT

PAGE = REPO_ROOT / "frontend" / "archives.html"
CHAT_SUFFIX = ".chat_parsed.json"
TRANSCRIPT_SUFFIX = ".transcript.json"  # backend.asr
ZH_SUFFIX = ".zh.transcript.json"  # backend.translate (Chinese subtitles)
HIGHLIGHTS_SUFFIX = ".highlights.json"  # backend.analysis (路灯表)
HIGHLIGHTS_PAGE = REPO_ROOT / "frontend" / "highlights.html"
RAW_CHAT_SUFFIXES = (".live_chat.json", ".chat.json")
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# chat_parsed path -> (mtime_ns, message count)
_count_cache: dict[Path, tuple[int, int]] = {}


def _seconds(t: str) -> int:
    """"HH:MM:SS" / "MM:SS" -> seconds; bad input -> 0."""
    try:
        total = 0
        for part in t.split(":"):
            total = total * 60 + int(part)
        return total
    except (ValueError, AttributeError):
        return 0


def _load_chat(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _chat_count(path: Path) -> int:
    mtime = path.stat().st_mtime_ns
    cached = _count_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        n = len(_load_chat(path))
    except (OSError, ValueError):
        n = 0
    _count_cache[path] = (mtime, n)
    return n


def _video_file(files: list[Path]) -> Path | None:
    """The finished mp4 (largest, if yt-dlp left format fragments around)."""
    mp4s = [f for f in files if f.suffix == ".mp4"]
    return max(mp4s, key=lambda f: f.stat().st_size) if mp4s else None


def _archive(root: Path, folder: Path) -> dict:
    files = [f for f in folder.iterdir() if f.is_file()]
    video = _video_file(files)
    chat = next((f for f in files if f.name.endswith(CHAT_SUFFIX)), None)
    raw_chat = next((f for f in files if f.name.endswith(RAW_CHAT_SUFFIXES)), None)
    transcript = transcript_file(files, "ja")
    creator, date, title = folder.relative_to(root).parts
    return {
        "creator": creator,
        "date": date,
        "title": title,
        "folder": folder.relative_to(root).as_posix(),
        "video": video.name if video else None,
        "video_size": video.stat().st_size if video else 0,
        "downloading": video is None or any(f.suffix == ".part" for f in files),
        "has_chat": chat is not None,
        "chat_file": chat.name if chat else None,
        "raw_chat_file": raw_chat.name if raw_chat else None,
        "chat_count": _chat_count(chat) if chat else 0,
        "transcript_file": transcript.name if transcript else None,
        "zh_transcript_file": f.name if (f := transcript_file(files, "zh")) else None,
        "highlights_file": next((f.name for f in files if f.name.endswith(HIGHLIGHTS_SUFFIX)), None),
        "size": sum(f.stat().st_size for f in files),
        "mtime": max((f.stat().st_mtime for f in files), default=folder.stat().st_mtime),
    }


def transcript_file(files: Iterable[Path], lang: str) -> Path | None:
    """The Japanese (`.transcript.json`) or Chinese (`.zh.transcript.json`) transcript."""
    if lang == "zh":
        return next((f for f in files if f.name.endswith(ZH_SUFFIX)), None)
    return next((f for f in files if f.name.endswith(TRANSCRIPT_SUFFIX) and not f.name.endswith(ZH_SUFFIX)), None)


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


def list_archives(root: Path) -> list[dict]:
    """Every `<creator>/<date>/<title>/` folder, newest first."""
    if not root.is_dir():
        return []
    rows = []
    for folder in root.glob("*/*/*"):
        if folder.is_dir():
            try:
                rows.append(_archive(root, folder))
            except OSError:
                continue  # vanished mid-scan (e.g. yt-dlp renaming)
    rows.sort(key=lambda r: (r["date"], r["mtime"]), reverse=True)
    return rows


def create_router(output_dir: Path,
                  visible: Callable[[Request], set[str] | None] = lambda request: None) -> APIRouter:
    """`visible(request)`: the streamer (creator folder) names that request may see, or
    None for everyone (no login, admins, members allowed "all")."""
    router = APIRouter()
    root = output_dir.resolve()

    def may_see(request: Request, creator: str) -> bool:
        names = visible(request)
        return names is None or creator in names

    def resolve_folder(folder: str, request: Request) -> Path:
        path = (root / folder).resolve()
        if path == root or not path.is_relative_to(root) or not path.is_dir():
            raise HTTPException(404, "archive not found")
        if not may_see(request, path.relative_to(root).parts[0]):
            raise HTTPException(404, "archive not found")  # same answer as "doesn't exist"
        return path

    def seen_archives(request: Request) -> list[dict]:
        return [a for a in list_archives(root) if may_see(request, a["creator"])]

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
        return {"output_dir": str(root), "disk": disk, "archives": seen_archives(request)}

    @router.get("/api/archives/chat")
    def chat(folder: str, request: Request) -> dict:
        path = resolve_folder(folder, request)
        file = next((f for f in path.iterdir() if f.name.endswith(CHAT_SUFFIX)), None)
        if not file:
            raise HTTPException(404, "no parsed chat")
        try:
            messages = _load_chat(file)
        except ValueError:
            raise HTTPException(500, "chat file is not valid JSON")
        density: list[int] = []
        for m in messages:
            minute = _seconds(m.get("time", "")) // 60
            if minute >= len(density):
                density.extend([0] * (minute + 1 - len(density)))
            density[minute] += 1
        return {"messages": messages, "density": density}

    def load_transcript(folder: str, lang: str, request: Request) -> list[dict]:
        path = resolve_folder(folder, request)
        file = transcript_file(path.iterdir(), lang)
        if not file:
            raise HTTPException(404, "no transcript")
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except ValueError:
            raise HTTPException(500, "transcript is not valid JSON")

    # ---- 路灯表 ----

    def highlights_file(path: Path) -> Path | None:
        return next((f for f in path.iterdir() if f.name.endswith(HIGHLIGHTS_SUFFIX)), None)

    @router.get("/highlights", include_in_schema=False)
    def highlights_page() -> FileResponse:
        return FileResponse(HIGHLIGHTS_PAGE)

    @router.get("/api/highlights")
    def highlights_list(request: Request) -> dict:
        """Every archive that has a 路灯表, newest first, with a one-line summary."""
        tables = []
        for a in seen_archives(request):
            path = root / a["folder"]
            file = highlights_file(path)
            if not file:
                continue
            try:
                rows = json.loads(file.read_text(encoding="utf-8")).get("rows") or []
            except (OSError, ValueError):
                continue
            scores = [r["score"] for r in rows if isinstance(r.get("score"), int)]
            tables.append({
                "folder": a["folder"], "creator": a["creator"], "date": a["date"], "title": a["title"],
                "segments": len(rows),
                "best_score": max(scores, default=None),
                "picks": sum(1 for r in rows if r.get("top_pick")),
            })
        return {"tables": tables}

    @router.get("/api/highlights/table")
    def highlights_table(folder: str, request: Request) -> dict:
        file = highlights_file(resolve_folder(folder, request))
        if not file:
            raise HTTPException(404, "no 路灯表 for this archive")
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except ValueError:
            raise HTTPException(500, "路灯表 is not valid JSON")

    @router.get("/api/highlights/xlsx")
    def highlights_xlsx_download(folder: str, request: Request) -> Response:
        """The 路灯表 as a formatted Excel sheet."""
        file = highlights_file(resolve_folder(folder, request))
        if not file:
            raise HTTPException(404, "no 路灯表 for this archive")
        try:
            table = json.loads(file.read_text(encoding="utf-8"))
        except ValueError:
            raise HTTPException(500, "路灯表 is not valid JSON")
        name = file.name.removesuffix(HIGHLIGHTS_SUFFIX) + ".highlights.xlsx"
        return Response(highlights_xlsx(table), media_type=XLSX_TYPE,
                        headers={"Content-Disposition": attachment(name)})

    @router.get("/api/archives/zip")
    def zip_download(folder: str, request: Request) -> StreamingResponse:
        """Everything of one archive except the raw chat, zipped on the fly (no temp file)."""
        path = resolve_folder(folder, request)
        files = sorted(f for f in path.iterdir() if f.is_file() and f.suffix != ".part"
                       and not f.name.endswith(RAW_CHAT_SUFFIXES))
        extra: dict[str, bytes] = {}
        if table := highlights_file(path):
            try:
                xlsx_name = table.name.removesuffix(HIGHLIGHTS_SUFFIX) + ".highlights.xlsx"
                extra[xlsx_name] = highlights_xlsx(json.loads(table.read_text(encoding="utf-8")))
            except ValueError:
                pass
        if zh := transcript_file(files, "zh"):
            try:
                extra[zh.name.removesuffix(ZH_SUFFIX) + ".zh.srt"] = srt(json.loads(zh.read_text(encoding="utf-8"))).encode()
            except ValueError:
                pass
        return StreamingResponse(stream_zip(files, extra), media_type="application/zip",
                                 headers={"Content-Disposition": attachment(path.name + ".zip")})

    @router.get("/api/archives/transcript")
    def transcript(folder: str, request: Request, lang: str = "ja") -> dict:
        return {"segments": load_transcript(folder, lang, request)}

    @router.get("/api/archives/srt")
    def srt_download(folder: str, request: Request, lang: str = "ja") -> Response:
        """Subtitles as an .srt file (`lang` ja or zh)."""
        name = resolve_folder(folder, request).name + (".zh.srt" if lang == "zh" else ".ja.srt")
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
        base = resolve_folder(folder, request)
        path = (base / name).resolve()
        if Path(name).name != name or path.parent != base:
            raise HTTPException(400, "bad file name")
        if not path.is_file():
            raise HTTPException(404, "file not found")
        # FileResponse handles Range requests, so <video> can seek.
        return FileResponse(path, filename=name, content_disposition_type="attachment" if download else "inline")

    return router
