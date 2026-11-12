"""The last messages sent on every channel, for the 通知 page (data/notify_log.json)."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.state_file import StateFile

log = logging.getLogger(__name__)


def decode_entries(raw: Any) -> list[dict[str, Any]]:
    """The history file's JSON -> entries; raises if it isn't a list of entries."""
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise ValueError("expected a list of entries")
    return raw


class NotifyHistory:
    """Newest first, at most `keep` entries. `path=None` keeps them in memory only."""

    def __init__(self, path: Path | None = None, keep: int = 50):
        self.path, self.keep = path, keep
        self._lock = threading.Lock()
        # Runtime state: a damaged file is set aside as a backup (the owner is alerted).
        self._store = StateFile(path, default=list, policy="recover", decode=decode_entries) if path else None
        self._entries: list[dict[str, Any]] = self._store.load()[:keep] if self._store else []

    def add(self, channel: str, target: str, summary: str, ok: bool, error: str = "") -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(), "channel": channel, "target": target,
                 "summary": summary[:200], "ok": ok, "error": error[:300]}
        with self._lock:
            self._entries = [entry, *self._entries][: self.keep]
            if self._store:
                try:
                    self._store.save(self._entries)
                except OSError:
                    log.exception("could not save %s", self.path)

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)
