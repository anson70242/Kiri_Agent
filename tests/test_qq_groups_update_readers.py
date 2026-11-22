"""A group's settings change in one step for the readers on the event loop: while the 通知
page saves a group (in the API's thread pool), `targets` and `recording` never see it half
changed or empty, so no message and no source record sheet row is missed. Made-up ids."""

from __future__ import annotations

import sys
import threading
import time

from backend.monitor import LiveState, Streamer, StreamStatus
from backend.notify.base import EventKind, StreamEvent
from backend.notify.qq import QQGroups, SourceRecordSheet

A = Streamer("A", "youtube", "@a")
SHEET = SourceRecordSheet("FAKEFILE3", "tab003")


def test_readers_never_see_a_group_half_saved(tmp_path, monkeypatch):
    groups = QQGroups(tmp_path / "qq_groups.json")
    groups.register("G1")
    groups.update("G1", source_record_sheet=SHEET, enabled=True, streamers=["A"], kinds=["ready"])
    monkeypatch.setattr(groups, "save", lambda: None)  # the readers race the change, not the file
    event = StreamEvent(EventKind.READY, StreamStatus(A, LiveState.OFFLINE))
    missed: list[str] = []
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set():
            try:
                if not groups.targets(event):
                    missed.append("targets")
                if not groups.recording(A):
                    missed.append("recording")
            except Exception as e:  # noqa: BLE001 - a group read half changed
                missed.append(repr(e))

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads as often as possible: the race shows at once
    reader = threading.Thread(target=read)
    reader.start()
    end = time.monotonic() + 1
    try:
        i = 0
        while not missed and time.monotonic() < end:
            i += 1
            groups.update("G1", name=f"组{i}")
    finally:
        stop.set()
        reader.join()
        sys.setswitchinterval(old)
    assert missed == []
