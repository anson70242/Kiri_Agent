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

Incidents are always logged, then go to the hook the application installed with
`set_incident_hook` (the operator alert; for a command line tool, `print_incidents`: the
terminal). This module knows nothing about the alert channels.

A store shared with another process re-reads its file with `reload`. A strict file found
damaged there never stops the running service: the last good value stays, and an Incident
without a backup is reported once.
"""

from __future__ import annotations

import json
import logging
import sys
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

    @property
    def message(self) -> str:
        """One readable line for the owner: command lines print it instead of a traceback."""
        return f"{self.path} 已损坏，无法读取（{self.reason}）。请修复这个文件，或从备份恢复后再运行。"


@dataclass(frozen=True)
class Incident:
    """A damaged runtime state file was set aside as `backup` and the store started empty.
    `backup` None: a strict file went bad while running; it was left in place and its
    store runs on the last good value it loaded."""

    path: Path
    backup: Path | None
    reason: str

    @property
    def message(self) -> str:
        """The operator alert's text (the alert path adds its own warning sign)."""
        if self.backup is None:
            return f"{self.path.name} 损坏，继续使用损坏前读到的内容，文件未改动，请修复或从备份恢复"
        return f"{self.path.name} 损坏，已备份为 {self.backup.name}，已按空文件继续运行"


IncidentHook = Callable[[Incident], None]

_hook: IncidentHook | None = None


def set_incident_hook(hook: IncidentHook | None) -> IncidentHook | None:
    """Where incidents go (None: only logged). Returns the hook it replaces."""
    global _hook
    previous, _hook = _hook, hook
    return previous


def print_incidents() -> IncidentHook | None:
    """For a command line tool, whose user is the owner at the terminal: every incident's
    message goes to stderr. Call it first thing, before any store loads. Returns the hook
    it replaces."""
    return set_incident_hook(_print_incident)


def _print_incident(incident: Incident) -> None:
    print(incident.message, file=sys.stderr)


def _report(incident: Incident) -> None:
    """Hand an incident to the installed hook (None: nowhere). Never raises: reporting
    must never break the store. The caller logs it."""
    if _hook:
        try:
            _hook(incident)
        except Exception:  # noqa: BLE001
            log.exception("could not report the incident with %s", incident.path)


def expect_list(raw: Any, what: str) -> list[Any]:
    """`raw` when it is a list; else the ValueError a `decode` raises for the wrong shape
    (`what`: the records the file holds, e.g. "users")."""
    if not isinstance(raw, list):
        raise ValueError(f"expected a list of {what}, got {type(raw).__name__}")
    return raw


def _same(x: Any) -> Any:
    return x


class StateFile(Generic[T]):
    """One JSON state file. `decode` turns the parsed JSON into the caller's value (raise
    on the wrong shape), `encode` turns it back into something json.dumps takes."""

    def __init__(self, path: Path, default: Callable[[], T], policy: Policy,
                 decode: Callable[[Any], T] = _same, encode: Callable[[T], Any] = _same):
        self.path = path
        self.default = default
        self.policy = policy
        self.decode = decode
        self.encode = encode
        self._stamp: tuple[int, int] | None = None  # the file as last loaded, tried or saved
        self.damaged: str | None = None  # strict: why the file could not be read when last tried

    def load(self) -> T:
        """The file's value; default() if it doesn't exist. Damaged: see the policy."""
        self._stamp = self._disk_stamp()
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._stamp, self.damaged = None, None
            return self.default()
        except (OSError, ValueError) as e:  # unreadable, or not UTF-8
            return self._damaged(e)
        try:
            value = self.decode(json.loads(text))
        except Exception as e:  # noqa: BLE001 - not JSON, or not the records the caller expects
            return self._damaged(e)
        self.damaged = None
        return value

    def reload(self, current: T) -> T:
        """The file's value when it changed on disk since this object last loaded, tried or
        saved it (another process rewrote it); else `current`, without reading the file. A
        damaged file is tried once until it changes again.

        Damaged under the recover policy: as in `load`. Under the strict policy this never
        raises: `current` (the last good value) stays in effect, the file is left alone, and
        it is logged and reported once (an Incident without a backup); `damaged` says why
        until a later read succeeds."""
        if not self.changed():
            return current
        was_damaged = self.damaged is not None
        try:
            return self.load()
        except StateFileError as e:
            if not was_damaged:
                log.error("%s is damaged (%s); kept the value read before", e.path, e.reason)
                _report(Incident(e.path, None, e.reason))
            return current

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
        self._stamp, self.damaged = self._disk_stamp(), None

    def changed(self) -> bool:
        """Whether the file on disk is no longer the one this object last loaded, tried to
        load or saved (another process rewrote it): the cue to load() again."""
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
            self.damaged = reason
            raise StateFileError(self.path, reason) from error
        backup = self._backup_name()
        self.path.rename(backup)
        self._stamp = None
        log.error("%s is damaged (%s); kept it as %s and continued empty", self.path, reason, backup.name)
        _report(Incident(self.path, backup, reason))
        return self.default()

    def _backup_name(self) -> Path:
        base = f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        backup, n = self.path.with_name(base), 1
        while backup.exists():
            n += 1
            backup = self.path.with_name(f"{base}-{n}")
        return backup
