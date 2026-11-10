"""Delivery: retries, permanent failures, ordering, never blocking the monitor."""

import asyncio
import time

from backend.notify import LiveNotifier, PermanentNotifyError

from .helpers import LIVE, OFFLINE, B, Recorder, ev, live, off


def make(*recs, **kw):
    kw.setdefault("retry_base_delay", 0.01)
    kw.setdefault("offline_grace", 0)
    return LiveNotifier(list(recs), **kw)


async def live_then_off(n, s_live=None, s_off=None):
    await n.on_change(off(), None)
    await n.on_change(s_live or live(), off())
    await n.on_change(s_off or off(), live())
    await asyncio.sleep(0.02)
    await n.drain()


def test_transient_failures_are_retried():
    async def go():
        rec = Recorder(fail=3)
        n = make(rec)
        await live_then_off(n)
        await n.aclose()
        assert rec.events == [ev(LIVE), ev(OFFLINE)]
        assert rec.calls == 5

    asyncio.run(go())


def test_permanent_failure_is_not_retried_and_next_event_still_sent():
    async def go():
        rec = Recorder(fail=1, exc=PermanentNotifyError)
        n = make(rec)
        await live_then_off(n)
        await n.aclose()
        assert rec.events == [ev(OFFLINE)]  # LIVE dropped, OFFLINE delivered
        assert rec.calls == 2

    asyncio.run(go())


def test_gives_up_after_max_attempts_then_continues():
    async def go():
        rec = Recorder(fail=4)
        n = make(rec, max_attempts=3)
        await live_then_off(n)
        await n.aclose()
        # LIVE: 3 failed tries. OFFLINE: 1 failure then success.
        assert rec.events == [ev(OFFLINE)]
        assert rec.calls == 5

    asyncio.run(go())


def test_order_is_kept_while_retrying():
    async def go():
        rec = Recorder(fail=2)
        n = make(rec)
        await live_then_off(n)
        await n.on_change(live(sid="v2"), off())
        await n.drain()
        await n.aclose()
        assert rec.events == [ev(LIVE), ev(OFFLINE), ev(LIVE, "v2")]

    asyncio.run(go())


def test_one_failing_channel_does_not_affect_another():
    async def go():
        bad, good = Recorder(fail=99, exc=PermanentNotifyError), Recorder()
        n = make(bad, good)
        await live_then_off(n)
        await n.aclose()
        assert good.events == [ev(LIVE), ev(OFFLINE)]
        assert bad.events == []

    asyncio.run(go())


def test_slow_channel_never_blocks_on_change():
    async def go():
        rec = Recorder(delay=0.5)
        n = make(rec)
        await n.on_change(off(), None)
        t = time.perf_counter()
        await n.on_change(live(), off())
        await n.on_change(off(B), None)
        assert time.perf_counter() - t < 0.1  # returned immediately
        await n.drain()
        await n.aclose()
        assert rec.events == [ev(LIVE)]

    asyncio.run(go())


def test_close_waits_for_queued_events():
    async def go():
        rec = Recorder(delay=0.05)
        n = make(rec)
        await n.on_change(off(), None)
        await n.on_change(live(), off())
        await n.aclose()  # no drain() first
        assert rec.events == [ev(LIVE)]

    asyncio.run(go())
