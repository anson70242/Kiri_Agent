"""The streamer list: who is monitored, and per-streamer settings (the dashboard's gear):
auto_download, keep, upload (to Tencent Docs), upload_sheet (the spreadsheet id),
drive (upload drive copies to Quark), drive_folder (where on the drive; blank: 录播/<name>,
the name as the archives are filed) and flash (put each live on QQ's flash transfer; absent
means off).

config.json is replaced on every deploy, so the list the dashboard edits lives in
its own file (`monitor.streamers_file`, default `data/streamers.json` next to the
config). config.json's `monitor.streamers` only seeds that file the first time.

The file is operator data: when it is damaged, loading raises StateFileError (the
service refuses to start) rather than run on an empty or re-seeded list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.archive import creator_folder
from backend.state_file import StateFile, expect_list
from backend.tencent_docs import parse_file_id

from .base import PlatformMonitor, Streamer


class RosterError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def entry_id(e: dict[str, Any]) -> str:
    return f"{e['platform']}:{e['channel']}"


def default_drive_folder(creator: str) -> str:
    """Where a blank drive folder points: `录播/` and the streamer's current name as their
    archives are filed (one valid folder, whatever the name holds)."""
    return f"录播/{creator_folder(creator)}"


def drive_folder(e: dict[str, Any]) -> str:
    return e.get("drive_folder") or default_drive_folder(e.get("name") or e["channel"])


def drive_folder_as_typed(text: str) -> str:
    """A drive folder as the owner typed it (the gear, the upload command's `--folder`), as
    stored: surrounding spaces and slashes removed, both kinds of slash as `/` (so a
    backslash never stays inside a part); blank stays blank. Raises ValueError with the
    reason, in Chinese, for a folder a drive cannot have. Which characters Quark itself
    refuses is not verified: this is the safe default."""
    folder = text.replace("\\", "/").strip(" /")
    for part in folder.split("/") if folder else ():
        if not part:
            raise ValueError("网盘文件夹里不能有空的一级（//）")
        if part in (".", ".."):
            raise ValueError("网盘文件夹里不能有 . 或 .. 这一级")
        if bad := [c for c in ':*?"<>|' if c in part]:
            raise ValueError(f"网盘文件夹名不能含有 {' '.join(bad)}")
    return folder


def decode_streamers(raw: Any) -> list[dict[str, Any]]:
    """The file's entries; raises on anything that isn't a list of streamers."""
    for e in expect_list(raw, "streamers"):
        if not (isinstance(e, dict) and isinstance(e.get("platform"), str) and isinstance(e.get("channel"), str)):
            raise ValueError(f"not a streamer (needs platform and channel): {e!r}")
    return raw


class Roster:
    def __init__(self, path: Path, seed: list[dict[str, Any]]):
        self._file: StateFile[list[dict[str, Any]] | None] = StateFile(
            path, default=lambda: None, policy="strict", decode=decode_streamers)
        entries = self._file.load()
        if entries is None:  # no file yet: seed it from the config
            self.entries: list[dict[str, Any]] = [dict(e) for e in seed]
            self.save()
        else:
            self.entries = entries

    def save(self) -> None:
        self._file.save(self.entries)

    def find(self, sid: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if entry_id(e) == sid), None)

    def add(self, name: str, platform: str, channel: str) -> dict[str, Any]:
        """Append a streamer (not saved until `save()`). Raises RosterError."""
        channel, name = channel.strip(), name.strip()
        if not channel:
            raise RosterError(400, "频道不能为空")
        if platform not in PlatformMonitor.registry:
            known = ", ".join(sorted(PlatformMonitor.registry))
            raise RosterError(400, f"不支持的平台: {platform}（支持: {known}）")
        entry = {"name": name or channel, "platform": platform, "channel": channel}
        if self.find(entry_id(entry)):
            raise RosterError(409, "这个主播已经在名单里了")
        self.entries.append(entry)
        return entry

    def update(self, sid: str, *, flash_available: bool = False, **settings: Any) -> dict[str, Any]:
        """Change per-streamer settings (the gear); None values are left alone.
        `flash_available`: a flash service is configured. Without one the flash box cannot be
        ticked; unticking it, or saving it as it stands, is always allowed."""
        entry = self.find(sid)
        if entry is None:
            raise RosterError(404, "没有这个主播")
        if settings.get("flash") and not entry.get("flash") and not flash_available:
            raise RosterError(422, "没有设置闪传服务，不能开启自动上传到 QQ 闪传")
        new = entry | {k: v for k, v in settings.items() if v is not None}
        if "upload_sheet" in settings and settings["upload_sheet"] is not None:
            text = settings["upload_sheet"].strip()
            new["upload_sheet"] = parse_file_id(text) if text else ""
            if new["upload_sheet"] is None:
                raise RosterError(422, "不是腾讯文档表格的链接（应为 docs.qq.com/sheet/…）")
        if settings.get("drive_folder") is not None:
            try:
                new["drive_folder"] = drive_folder_as_typed(settings["drive_folder"])
            except ValueError as e:
                raise RosterError(422, str(e)) from None
        if new.get("upload") and not new.get("upload_sheet"):
            raise RosterError(422, "开启自动上传前先填目标表格")
        entry.update(new)
        return entry

    def remove(self, sid: str) -> None:
        """Stop monitoring a streamer. Their downloaded archives are left alone."""
        entry = self.find(sid)
        if entry is None:
            raise RosterError(404, "没有这个主播")
        self.entries.remove(entry)

    def keep_map(self, default: int) -> dict[str, int]:
        """Streamer id -> archives to keep, for the auto-downloading streamers only."""
        return {entry_id(e): int(e.get("keep") or default) for e in self.entries if e.get("auto_download")}

    def upload_map(self) -> dict[str, str]:
        """Streamer id -> Tencent Docs spreadsheet id, for the streamers that auto-upload."""
        return {entry_id(e): e["upload_sheet"] for e in self.entries if e.get("upload") and e.get("upload_sheet")}

    def drive_map(self) -> dict[str, str]:
        """Streamer id -> folder on the Quark drive, for the streamers whose drive box is ticked."""
        return {entry_id(e): drive_folder(e) for e in self.entries if e.get("drive")}

    def flash_streamers(self) -> set[str]:
        """The ids of the streamers whose flash box is ticked (whether a flash service is
        configured is not asked here)."""
        return {entry_id(e) for e in self.entries if e.get("flash")}

    def streamers(self) -> list[Streamer]:
        return [Streamer(e.get("name") or e["channel"], e["platform"], e["channel"]) for e in self.entries]

    def to_dicts(self) -> list[dict[str, Any]]:
        return [{"id": entry_id(e), **e, "auto_download": bool(e.get("auto_download")),
                 "upload": bool(e.get("upload")), "upload_sheet": e.get("upload_sheet") or "",
                 "drive": bool(e.get("drive")), "drive_folder": e.get("drive_folder") or "",
                 "flash": bool(e.get("flash")),
                 "drive_folder_default": default_drive_folder(e.get("name") or e["channel"])} for e in self.entries]


def load_roster(config: Path) -> Roster:
    cfg = json.loads(config.read_text(encoding="utf-8")).get("monitor") or {}
    path = Path(cfg.get("streamers_file") or "data/streamers.json")
    return Roster(path if path.is_absolute() else config.parent / path, cfg.get("streamers") or [])
