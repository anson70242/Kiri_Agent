"""Randomized stress test: whatever the platforms report, events stay consistent.

Invariants per streamer:
- events strictly alternate LIVE, OFFLINE, LIVE, ...
- each OFFLINE is for the same broadcast as the LIVE before it
- once everything goes offline and the grace passes, the last event is OFFLINE
- no broadcast is announced LIVE twice in a row
"""

import asyncio
import os
import random

from backend.monitor import LiveState, StreamMonitor, Streamer
from backend.notify import LiveNotifier

from .helpers import LIVE, OFFLINE, Recorder
from .test_monitor import SCRIPT, FakeMonitor  # noqa: F401  (registers "fake")

GRACE = 0.01
CHOICES = [LiveState.OFFLINE, LiveState.UPCOMING, "err", "v1", "v2", "v3", None]


def random_script(rng: random.Random, n: int) -> list:
    out = []
    for _ in range(n):
        c = rng.choice(CHOICES)
        if c == "err":
            out.append(RuntimeError("random failure"))
        elif isinstance(c, LiveState):
            out.append(c)
        else:
            out.append(("live", c))  # c None = live without a stream id
    return out


def check_invariants(events, label):
    for i, (kind, _, sid) in enumerate(events):
        assert kind is (LIVE if i % 2 == 0 else OFFLINE), f"{label}: not alternating: {events}"
        if kind is OFFLINE:
            assert sid == events[i - 1][2], f"{label}: OFFLINE for a different stream: {events}"
    if events:
        assert events[-1][0] is OFFLINE, f"{label}: never reported ended: {events}"


def test_random_sequences_keep_events_consistent():
    async def one(seed: int):
        rng = random.Random(seed)
        streamers = [Streamer(f"S{i}", "fake", f"r{seed}_{i}") for i in range(3)]
        for s in streamers:
            SCRIPT[s.channel] = random_script(rng, 15) + [LiveState.OFFLINE] * 3
        rec = Recorder(fail=rng.randint(0, 3))  # some delivery failures too
        notifier = LiveNotifier([rec], notify_on_startup=True, offline_grace=GRACE,
                                retry_base_delay=0.001)
        async with StreamMonitor(streamers) as mon:
            mon.on_change.append(notifier.on_change)
            for _ in range(18):
                await mon.refresh()
                if rng.random() < 0.3:
                    await asyncio.sleep(GRACE * 2)  # sometimes let a grace window expire
            await asyncio.sleep(GRACE * 5)
        await notifier.aclose()
        for s in streamers:
            check_invariants([e for e in rec.events if e[1] == s.id], f"seed {seed} {s.name}")

    async def all_seeds():
        for seed in range(int(os.getenv("STRESS_SEEDS", "100"))):
            await one(seed)

    asyncio.run(all_seeds())
