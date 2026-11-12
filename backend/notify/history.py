"""The last messages sent on every channel, for the 通知 page (data/notify_log.json)."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class NotifyHistory:
    """Newest first, at most `keep` entries. `path=None` keeps them in memory only."""

    def __init__(self, path: Path | None = None, keep: int = 50):
        self.path, self.keep = path, keep
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        if path and path.exists():
            try:
                self._entries = json.loads(path.read_text(encoding="utf-8"))[:keep]
            except (OSError, ValueError):
                log.exception("could not read %s; starting empty", path)

    def add(self, channel: str, target: str, summary: str, ok: bool, error: str = "") -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(), "channel": channel, "target": target,
                 "summary": summary[:200], "ok": ok, "error": error[:300]}
        with self._lock:
            self._entries = [entry, *self._entries][: self.keep]
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self._entries, ensure_ascii=False, indent=1), encoding="utf-8")
                    os.replace(tmp, self.path)
                except OSError:
                    log.exception("could not save %s", self.path)

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)
