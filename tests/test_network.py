"""Real YouTube / Twitch checks. Run with: uv run pytest -m network"""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from backend.monitor import LiveState, StreamMonitor, Streamer

pytestmark = pytest.mark.network

CONFIG = Path(__file__).resolve().parents[1] / "config.json"
EXTRA = [
    Streamer("Lofi Girl", "youtube", "@LofiGirl"),  # 24/7 live
    Streamer("YouTube", "youtube", "@YouTube"),  # normally offline
    Streamer("lirik", "twitch", "lirik"),
]


def streamers():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))["monitor"]["streamers"]
    return [Streamer(s["name"], s["platform"], s["channel"]) for s in cfg] + EXTRA


def test_repeated_real_checks_are_stable():
    """Poll every streamer several times: no failures, and no state flip-flopping."""
    rounds = 5

    async def go():
        seen: dict[str, list] = {}
        async with StreamMonitor(streamers()) as mon:
            for _ in range(rounds):
                for st in await mon.poll_once():
                    seen.setdefault(st.streamer.id, []).append(st)
                await asyncio.sleep(3)
        return seen

    seen = asyncio.run(go())
    for sid, results in seen.items():
        states = Counter(r.state for r in results)
        errors = [r.error for r in results if r.error]
        print(sid, dict(states), errors[:1])
        assert LiveState.UNKNOWN not in states, f"{sid}: check failed: {errors}"
        assert len(states) == 1, f"{sid}: state flip-flopped: {states}"
        live = [r for r in results if r.is_live]
        assert len({r.stream_id for r in live}) <= 1, f"{sid}: stream id changed: {[r.stream_id for r in live]}"
        for r in live:
            assert r.stream_id and r.title and r.url and r.started_at, f"{sid}: incomplete live status {r}"


def test_known_live_channel_is_live():
    async def go():
        async with StreamMonitor([EXTRA[0]]) as mon:
            return (await mon.poll_once())[0]

    st = asyncio.run(go())
    assert st.state is LiveState.LIVE, st
