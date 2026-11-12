"""The streamer list: who is monitored, and per-streamer settings (auto_download, keep).

config.json is replaced on every deploy, so the list the dashboard edits lives in
its own file (`monitor.streamers_file`, default `data/streamers.json` next to the
config). config.json's `monitor.streamers` only seeds that file the first time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import PlatformMonitor, Streamer


class RosterError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def entry_id(e: dict[str, Any]) -> str:
    return f"{e['platform']}:{e['channel']}"


class Roster:
    def __init__(self, path: Path, seed: list[dict[str, Any]]):
        self.path = path
        try:
            self.entries: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.entries = [dict(e) for e in seed]
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def find(self, sid: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if entry_id(e) == sid), None)

    def add(self, name: str, platform: str, channel: str) -> dict[str, Any]:
        """Append a streamer (not saved until `save()`). Raises RosterError."""
        channel, name = channel.strip(), name.strip()
        if not channel:
            raise RosterError(400, "频道不能为空")
        if platform not in PlatformMonitor.registry:
            raise RosterError(400, f"不支持的平台: {platform}")
        entry = {"name": name or channel, "platform": platform, "channel": channel}
        if self.find(entry_id(entry)):
            raise RosterError(409, "这个主播已经在名单里了")
        self.entries.append(entry)
        return entry

    def update(self, sid: str, **settings: Any) -> dict[str, Any]:
        """Change per-streamer settings (the gear); None values are left alone."""
        entry = self.find(sid)
        if entry is None:
            raise RosterError(404, "没有这个主播")
        entry.update({k: v for k, v in settings.items() if v is not None})
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

    def streamers(self) -> list[Streamer]:
        return [Streamer(e.get("name") or e["channel"], e["platform"], e["channel"]) for e in self.entries]

    def to_dicts(self) -> list[dict[str, Any]]:
        return [{"id": entry_id(e), **e, "auto_download": bool(e.get("auto_download"))} for e in self.entries]


def load_roster(config: Path) -> Roster:
    cfg = json.loads(config.read_text(encoding="utf-8")).get("monitor") or {}
    path = Path(cfg.get("streamers_file") or "data/streamers.json")
    return Roster(path if path.is_absolute() else config.parent / path, cfg.get("streamers") or [])
