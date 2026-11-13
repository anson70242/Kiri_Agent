"""A sitting survives a restart: after the service restarts in the middle of a sitting that
already continued past its first live, the one "ended" still covers the whole sitting."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

from backend.notify import LiveNotifier

from .helpers import LIVE, OFFLINE, T0, Recorder, ev, off
from .test_live_continues import cast

GRACE = 0.05


async def session(state_file, steps, grace=GRACE):
    """One run of the service on `state_file`: the notifications it delivered and the
    events the "a live ended" listeners got."""
    rec, ended = Recorder(), []
    n = LiveNotifier([rec], offline_grace=grace, state_file=state_file)
    n.on_ended.append(ended.append)
    for new, old in steps:
        await n.on_change(new, old)
    await asyncio.sleep(GRACE * 3)
    await n.drain()
    await n.aclose()
    return rec, ended


def run(state_file, steps, **kw):
    return asyncio.run(session(state_file, steps, **kw))


A, B = cast("a1", 0, seen=0), cast("b1", 2, seen=2)
STOPPED = replace(off(), checked_at=T0 + timedelta(minutes=50))
BEFORE_RESTART = [(off(), None), (A, off()), (B, A)]  # A announced, B continued it


def test_sitting_still_live_after_a_restart_ends_with_its_first_live_and_last_end_time(tmp_path):
    f = tmp_path / "s.json"
    first, _ = run(f, BEFORE_RESTART)
    second, ended = run(f, [(B, None), (STOPPED, B)])
    assert first.events == [ev(LIVE, "a1")]
    assert second.events == [ev(OFFLINE, "a1")]
    final = second.raw[-1]
    assert final.status.started_at == A.started_at
    assert final.ended_at == STOPPED.checked_at
    assert [(e.status.stream_id, e.status.started_at) for e in ended] == [("b1", B.started_at)]
