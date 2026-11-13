"""State file incidents -> the operator alert (the one that reports the QQ bot offline).

    alerts = IncidentAlerts()
    set_incident_hook(alerts)   # first thing at start-up, before any store loads
    ...                         # build the notifier, the stores...
    alerts.start(notifier)      # inside the running event loop
    ...
    alerts.stop()               # at shutdown

Incidents before `start` (stores load while the app is being built) or from worker
threads are held or handed to the event loop, so each reaches LiveNotifier.alert once
the loop runs. Without a notifier, or after `stop`, they are only logged (backend.state_file
logs every incident anyway).
"""

from __future__ import annotations

import asyncio
import logging
import threading

from backend.state_file import Incident

from .base import LiveNotifier

log = logging.getLogger(__name__)


class IncidentAlerts:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: list[str] | None = []  # incidents before `start`; None once started
        # where they go once started; None: nowhere (no alert channel, or stopped)
        self._target: tuple[asyncio.AbstractEventLoop, LiveNotifier] | None = None

    def __call__(self, incident: Incident) -> None:
        text = incident.message
        with self._lock:
            if self._held is not None:
                self._held.append(text)
                return
            target = self._target
        if target is None:
            return
        loop, notifier = target
        try:
            loop.call_soon_threadsafe(notifier.alert, text)  # alert() needs the loop's thread
        except RuntimeError as e:  # the loop closed meanwhile
            log.warning("alert not sent (%s): %s", e, text)

    def start(self, notifier: LiveNotifier | None) -> None:
        """Deliver held incidents and every later one through `notifier.alert` (None: no
        alert channel, only logged). Call from inside the running event loop."""
        with self._lock:
            held, self._held = self._held or [], None
            if notifier is None:
                return
            self._target = asyncio.get_running_loop(), notifier
        for text in held:
            notifier.alert(text)

    def stop(self) -> None:
        """From now on incidents are only logged (the notifier is shutting down)."""
        with self._lock:
            self._held, self._target = None, None
