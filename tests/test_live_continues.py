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


def test_new_live_at_the_same_address_at_the_next_check_continues_the_sitting():
    a, b = cast("a1", minutes=0, seen=0), cast("b1", minutes=1, seen=1)
    rec, ended = run([(off(), None), (a, off()), (b, a)], grace=1)
    assert rec.events == [ev(LIVE, "a1")]
    assert [(e.status.stream_id, e.status.title, e.status.started_at, e.ended_at) for e in ended] == [
        ("a1", "title a1", a.started_at, b.checked_at)]


def test_sitting_ends_with_one_ended_carrying_the_announced_live_and_the_last_end_time():
    a, b = cast("a1", minutes=0, seen=0), cast("b1", minutes=2, seen=2)
    stopped = replace(off(), checked_at=T0 + timedelta(minutes=50))
    rec, ended = run([(off(), None), (a, off()), (b, a), (stopped, b)])
    assert rec.events == [ev(LIVE, "a1"), ev(OFFLINE, "a1")]
    final = rec.raw[-1]
    assert final.status.started_at == a.started_at  # the duration covers the sitting
    assert final.ended_at == stopped.checked_at
    assert final.status.title == "title b1"  # what the streamer shows last
    assert [(e.status.stream_id, e.status.started_at, e.ended_at) for e in ended] == [
        ("a1", a.started_at, b.checked_at), ("b1", b.started_at, stopped.checked_at)]


def test_sitting_of_three_lives_sends_one_pair_and_reaches_the_listeners_three_times():
    a, b, c = cast("a1", 0, seen=0), cast("b1", 2, seen=2), cast("c1", 4, seen=4)
    rec, ended = run([(off(), None), (a, off()), (off(), a), (b, off()), (c, b), (off(), c)])
    assert rec.events == [ev(LIVE, "a1"), ev(OFFLINE, "a1")]
    assert [e.status.stream_id for e in ended] == ["a1", "b1", "c1"]
    assert [e.status.title for e in ended] == ["title a1", "title b1", "title c1"]


def test_new_live_at_a_different_address_is_announced_as_a_new_sitting():
    a, b = cast("a1", 0, address="https://x/a1"), cast("b1", 2, address="https://x/b1")
    rec, ended = run([(off(), None), (a, off()), (off(), a), (b, off())], grace=1)
    assert rec.events == [ev(LIVE, "a1"), ev(OFFLINE, "a1"), ev(LIVE, "b1")]
    assert [e.status.stream_id for e in ended] == ["a1"]


def test_new_live_after_the_grace_period_is_a_new_sitting():
    a, b = cast("a1", 0), cast("b1", 10)
    rec, ended = run([(off(), None), (a, off()), (off(), a), GRACE * 3, (b, off())])
    assert rec.events == [ev(LIVE, "a1"), ev(OFFLINE, "a1"), ev(LIVE, "b1")]
    assert [e.status.stream_id for e in ended] == ["a1"]


def test_live_without_an_id_never_looks_like_a_new_live():
    a, nameless = cast("a1", 0), replace(cast("a1", 0), stream_id=None)
    rec, ended = run([(off(), None), (a, off()), (nameless, a), (off(), nameless)])
    assert rec.events == [ev(LIVE, "a1"), ev(OFFLINE, "a1")]
    assert [e.status.stream_id for e in ended] == ["a1"]


# ---- across a restart ----

async def session(state_file, steps, grace=GRACE):
    """One run of the service: a fresh LiveNotifier on the same state file."""
    rec, ended = Recorder(), []
    n = LiveNotifier([rec], offline_grace=grace, state_file=state_file)
    n.on_ended.append(ended.append)
    for new, old in steps:
        await n.on_change(new, old)
    await asyncio.sleep(GRACE * 3)
    await n.drain()
    await n.aclose()
    return rec, ended


def test_saved_live_that_differs_after_a_restart_is_reported_ended_then_the_new_one_live(tmp_path):
    f = tmp_path / "s.json"
    a, b = cast("a1", 0), cast("b1", 2)
    asyncio.run(session(f, [(off(), None), (a, off())]))
    rec, ended = asyncio.run(session(f, [(b, None)]))
    assert rec.events == [ev(OFFLINE, "a1"), ev(LIVE, "b1")]
    assert [e.status.stream_id for e in ended] == ["a1"]


def test_saved_live_that_stops_after_a_restart_is_not_continued_by_a_new_live(tmp_path):
    f = tmp_path / "s.json"
    a, b = cast("a1", 0), cast("b1", 2)
    asyncio.run(session(f, [(off(), None), (a, off())]))
    rec, ended = asyncio.run(session(f, [(off(), None), (b, off())], grace=1))
    assert rec.events == [ev(OFFLINE, "a1"), ev(LIVE, "b1")]
    assert [e.status.stream_id for e in ended] == ["a1"]


def test_restart_inside_a_sitting_keeps_it_quiet_and_each_live_reaches_the_listeners_once(tmp_path):
    # The sitting's first live is not saved: after the restart the current live stands for
    # the sitting, so its "ended" carries that live's own id and start time.
    f = tmp_path / "s.json"
    a, b = cast("a1", 0), cast("b1", 2)
    first, first_ended = asyncio.run(session(f, [(off(), None), (a, off()), (b, a)]))
    second, second_ended = asyncio.run(session(f, [(b, None), (off(), b)]))
    assert first.events == [ev(LIVE, "a1")]
    assert second.events == [ev(OFFLINE, "b1")]
    assert [e.status.stream_id for e in first_ended + second_ended] == ["a1", "b1"]
