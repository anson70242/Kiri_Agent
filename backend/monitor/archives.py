"""Archives (录播) pages for the dashboard: browse downloaded VODs and their chat.

Downloads live in `<output_dir>/<creator>/<YYYYMMDD>/<title>/` (see
backend/downloader/base.py VideoInfo.folder). Every path a client sends is resolved
and must stay inside output_dir.
"""

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .__main__ import REPO_ROOT

PAGE = REPO_ROOT / "frontend" / "archives.html"
CHAT_SUFFIX = ".chat_parsed.json"
RAW_CHAT_SUFFIXES = (".live_chat.json", ".chat.json")

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
        "size": sum(f.stat().st_size for f in files),
        "mtime": max((f.stat().st_mtime for f in files), default=folder.stat().st_mtime),
    }


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


def create_router(output_dir: Path) -> APIRouter:
    router = APIRouter()
    root = output_dir.resolve()

    def resolve_folder(folder: str) -> Path:
        path = (root / folder).resolve()
        if path == root or not path.is_relative_to(root) or not path.is_dir():
            raise HTTPException(404, "archive not found")
        return path

    @router.get("/archives", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(PAGE)

    @router.get("/api/archives")
    def archives() -> dict:
        disk = None
        try:
            u = shutil.disk_usage(root if root.exists() else root.parent)
            disk = {"total": u.total, "used": u.used, "free": u.free}
        except OSError:
            pass
        return {"output_dir": str(root), "disk": disk, "archives": list_archives(root)}

    @router.get("/api/archives/chat")
    def chat(folder: str) -> dict:
        path = resolve_folder(folder)
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

    @router.get("/api/archives/file")
    def file(folder: str, name: str, download: bool = False) -> FileResponse:
        base = resolve_folder(folder)
        path = (base / name).resolve()
        if Path(name).name != name or path.parent != base:
            raise HTTPException(400, "bad file name")
        if not path.is_file():
            raise HTTPException(404, "file not found")
        # FileResponse handles Range requests, so <video> can seek.
        return FileResponse(path, filename=name, content_disposition_type="attachment" if download else "inline")

    return router
