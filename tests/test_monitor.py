"""StreamMonitor: change detection, error isolation, and end-to-end with LiveNotifier."""

import asyncio

from backend.monitor import LiveState, PlatformMonitor, StreamMonitor, Streamer, StreamStatus
from backend.notify import LiveNotifier

from .helpers import LIVE, OFFLINE, Recorder

# Scripted results per channel: a state, ("live", stream_id), or an Exception.
SCRIPT: dict[str, list] = {}


class FakeMonitor(PlatformMonitor):
    platform = "fake"

    async def _check(self, s: Streamer) -> StreamStatus:
        item = SCRIPT[s.channel].pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            return StreamStatus(s, LiveState.LIVE, url=f"https://x/{item[1]}", stream_id=item[1])
        return StreamStatus(s, item)


X = Streamer("X", "fake", "x")
Y = Streamer("Y", "fake", "y")
OFF, UP = LiveState.OFFLINE, LiveState.UPCOMING


def L(sid="v1"):
    return ("live", sid)


def ERR():
    return RuntimeError("network down")


async def poll(mon, n):
    out = []
    for _ in range(n):
        out.append([(new.state.value, old.state.value if old else None) for new, old in await mon.refresh()])
    return out


def test_failed_check_does_not_overwrite_known_state():
    async def go():
        SCRIPT["x"] = [OFF, ERR(), OFF, ERR(), L()]
        async with StreamMonitor([X]) as mon:
            changes = await poll(mon, 5)
        assert changes == [[("offline", None)], [], [], [], [("live", "offline")]]

    asyncio.run(go())


def test_new_broadcast_between_polls_is_a_change():
    async def go():
        SCRIPT["x"] = [L("v1"), L("v1"), L("v2"), L(None)]
        async with StreamMonitor([X]) as mon:
            changes = await poll(mon, 4)
        # v1->v1 same; v1->v2 new; v2->None (id missing) treated as same.
        assert changes == [[("live", None)], [], [("live", "live")], []]

    asyncio.run(go())


def test_broken_callback_does_not_stop_other_callbacks_or_refresh():
    async def go():
        SCRIPT["x"] = [OFF, L()]
        seen = []

        def bad(new, old):
            raise ValueError("bug in callback")

        async with StreamMonitor([X]) as mon:
            mon.on_change += [bad, lambda new, old: seen.append(new.state)]
            await mon.refresh()
            await mon.refresh()
        assert seen == [OFF, LiveState.LIVE]

    asyncio.run(go())


def test_run_loop_survives_refresh_errors():
    async def go():
        async with StreamMonitor([X]) as mon:
            calls = 0

            async def flaky_refresh():
                nonlocal calls
                calls += 1
                if calls <= 2:
                    raise RuntimeError("unexpected")
                return []

            mon.refresh = flaky_refresh
            task = asyncio.create_task(mon.run(0.01))
            await asyncio.sleep(0.2)
            task.cancel()
        assert calls > 3

    asyncio.run(go())


def test_one_streamer_failing_does_not_affect_another():
    async def go():
        SCRIPT["x"] = [ERR(), ERR(), ERR()]
        SCRIPT["y"] = [OFF, L(), OFF]
        async with StreamMonitor([X, Y]) as mon:
            await poll(mon, 3)
            assert mon.statuses[Y].state is OFF
            assert mon.statuses[X].state is LiveState.UNKNOWN

    asyncio.run(go())


def test_end_to_end_monitor_and_notifier():
    """A realistic sequence through the real monitor + notifier."""

    async def go():
        SCRIPT["x"] = [
            OFF,            # startup
            L("v1"),        # went live          -> LIVE v1
            ERR(),          # network error       (ignored)
            L("v1"),
            OFF,            # blip...
            L("v1"),        # ...back in grace    (nothing)
            L("v2"),        # restarted between polls -> OFFLINE v1, LIVE v2
            UP,             # ended, next scheduled  -> OFFLINE v2 after grace
            UP,
            L("v3"),        # -> LIVE v3
        ]
        rec = Recorder()
        notifier = LiveNotifier([rec], offline_grace=0.05)
        async with StreamMonitor([X]) as mon:
            mon.on_change.append(notifier.on_change)
            for _ in range(len(SCRIPT["x"])):
                await mon.refresh()
                await asyncio.sleep(0.04)  # < grace, so the blip is absorbed
            await asyncio.sleep(0.15)
        await notifier.aclose()
        assert rec.events == [
            (LIVE, "fake:x", "v1"),
            (OFFLINE, "fake:x", "v1"),
            (LIVE, "fake:x", "v2"),
            (OFFLINE, "fake:x", "v2"),
            (LIVE, "fake:x", "v3"),
        ]

    asyncio.run(go())
