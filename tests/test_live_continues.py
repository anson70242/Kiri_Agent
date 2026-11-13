"""A live that continues: a new live at the same watch address, inside the grace period or
straight after the previous one, belongs to the same sitting (LiveNotifier rules)."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from backend.notify import LiveNotifier

from .helpers import LIVE, OFFLINE, T0, Recorder, ev, live, off

GRACE = 0.05
ADDRESS = "https://example.test/channel"  # one watch address for every live of the channel


def cast(sid: str, minutes: int = 0, address: str = ADDRESS, seen: int | None = None):
    """Live `sid` at `address`, started `minutes` after T0 and seen at minute `seen`."""
    status = replace(live(sid=sid, started=T0 + timedelta(minutes=minutes)), url=address)
    if seen is not None:
        status = replace(status, checked_at=T0 + timedelta(minutes=seen))
    return status


async def run_steps(steps, grace=GRACE, **kw):
    """Feed (new, old) pairs, a number means sleep that long; return the delivered
    notifications and the events the "a live ended" listeners got."""
    rec = Recorder()
    ended = []
    n = LiveNotifier([rec], offline_grace=grace, **kw)
    n.on_ended.append(ended.append)
    for step in steps:
        if isinstance(step, (int, float)):
            await asyncio.sleep(step)
        else:
            await n.on_change(*step)
    await asyncio.sleep(GRACE * 3)  # lets a short grace period run out
    await n.drain()
    await n.aclose()
    return rec, ended


def run(steps, **kw):
    return asyncio.run(run_steps(steps, **kw))


def test_new_live_at_the_same_address_inside_the_grace_period_continues_the_sitting():
    a, b = cast("a1", minutes=0, seen=0), cast("b1", minutes=2, seen=2)
    rec, ended = run([(off(), None), (a, off()), (off(), a), (b, off())], grace=1)
    assert rec.events == [ev(LIVE, "a1")]
    assert len(ended) == 1
    assert ended[0].status.stream_id == "a1"
    assert ended[0].status.title == "title a1"
    assert ended[0].status.started_at == a.started_at
    assert ended[0].ended_at == b.checked_at
