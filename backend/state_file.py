"""State files: JSON files in the data directory holding state that survives restarts.

    store = StateFile(path, default=list, policy="recover",
                      decode=lambda raw: [Job.from_dict(d) for d in raw],
                      encode=lambda jobs: [j.to_dict() for j in jobs])
    jobs = store.load()   # missing file -> default()
    store.save(jobs)      # atomic: written to "<name>.tmp" beside it, then swapped in

A file is damaged when it exists but can't be read, isn't valid JSON, or `decode` fails
on it (the wrong shape). What happens then is the caller's policy:

- "strict" (operator data: roster, users, QQ groups): raise StateFileError naming the
  file and the reason; the file is left alone for a person to repair.
- "recover" (runtime state: queues, live-stream state, history, sessions): rename it to
  "<name>.corrupt-<YYYYMMDD-HHMMSS>", report an Incident, and return default().

Incidents are always logged, then go to the StateFile's own `on_incident`, or else to
the hook the application installed with `set_incident_hook` (the operator alert). This
module knows nothing about where an alert ends up.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
Policy = Literal["strict", "recover"]


class StateFileError(Exception):
    """A strict state file is damaged: the service must not run on a guess."""

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path} is damaged: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class Incident:
    """A damaged runtime state file was set aside as `backup` and the store started empty."""

    path: Path
    backup: Path
    reason: str

    @property
    def message(self) -> str:
        """The operator alert's text (the alert path adds its own warning sign)."""
        return f"{self.path.name} 损坏，已备份为 {self.backup.name}，已按空文件继续运行"


IncidentHook = Callable[[Incident], None]

_hook: IncidentHook | None = None


def set_incident_hook(hook: IncidentHook | None) -> IncidentHook | None:
    """Where incidents of StateFiles without their own `on_incident` go (None: only
    logged). Returns the hook it replaces."""
    global _hook
    previous, _hook = _hook, hook
    return previous


def _same(x: Any) -> Any:
    return x


class StateFile(Generic[T]):
    """One JSON state file. `decode` turns the parsed JSON into the owner's value (raise
    on the wrong shape), `encode` turns it back into something json.dumps takes."""

    def __init__(self, path: Path, default: Callable[[], T], policy: Policy,
                 decode: Callable[[Any], T] = _same, encode: Callable[[T], Any] = _same,
                 on_incident: IncidentHook | None = None):
        self.path = path
        self.default = default
        self.policy = policy
        self.decode = decode
        self.encode = encode
        self.on_incident = on_incident
        self._stamp: tuple[int, int] | None = None  # the file as last loaded or saved

    def load(self) -> T:
        """The file's value; default() if it doesn't exist. Damaged: see the policy."""
        stamp = self._disk_stamp()
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._stamp = None
            return self.default()
        except (OSError, ValueError) as e:  # unreadable, or not UTF-8
            return self._damaged(e)
        try:
            value = self.decode(json.loads(text))
        except Exception as e:  # noqa: BLE001 - not JSON, or not the records the owner expects
            return self._damaged(e)
        self._stamp = stamp
        return value

    def save(self, value: T) -> None:
        """Write atomically: never a half-written file, even if the process dies."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.encode(value), ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._stamp = self._disk_stamp()

    def changed(self) -> bool:
        """Whether the file on disk is no longer the one this object last loaded or saved
        (another process rewrote it): the cue to load() again."""
        return self._disk_stamp() != self._stamp

    def _disk_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return st.st_mtime_ns, st.st_size

    def _damaged(self, error: Exception) -> T:
        reason = f"{type(error).__name__}: {error}"
        if self.policy == "strict":
            raise StateFileError(self.path, reason) from error
        backup = self._backup_name()
        self.path.rename(backup)
        self._stamp = None
        incident = Incident(self.path, backup, reason)
        log.error("%s is damaged (%s); kept it as %s and continued empty", self.path, reason, backup.name)
        if hook := self.on_incident or _hook:
            try:
                hook(incident)
            except Exception:  # noqa: BLE001 - reporting must never break the store
                log.exception("could not report the incident with %s", self.path)
        return self.default()

    def _backup_name(self) -> Path:
        base = f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        backup, n = self.path.with_name(base), 1
        while backup.exists():
            n += 1
            backup = self.path.with_name(f"{base}-{n}")
        return backup
