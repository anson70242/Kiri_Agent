"""A sitting survives a restart: after the service restarts in the middle of a sitting,
whether it already continued past its first live or continues only after the restart,
the one "ended" still covers the whole sitting."""

import asyncio
import json
import os
import random
from dataclasses import replace
from datetime import timedelta

import pytest

from backend import check_state
from backend.monitor import LiveState, StreamMonitor, Streamer
from backend.notify import LiveNotifier

from .helpers import LIVE, OFFLINE, T0, Recorder, ev, off
from .test_check_state import qq_on, write_config  # noqa: F401  (qq_on is a fixture)
from .test_live_continues import FakeChannelMonitor, cast, random_lives  # noqa: F401  (registers "fake_channel")
from .test_monitor import SCRIPT
from .test_random_sequences import check_invariants
from .test_runtime_state_recovery import backups, incidents  # noqa: F401  (incidents is a fixture)

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


def test_sitting_whose_grace_period_ran_at_the_restart_ends_with_its_first_live(tmp_path):
    f = tmp_path / "s.json"
    run(f, BEFORE_RESTART + [(off(), B)], grace=10)  # stops before the grace period runs out
    second, ended = run(f, [(STOPPED, None)])
    assert second.events == [ev(OFFLINE, "a1")]
    assert second.raw[-1].status.started_at == A.started_at
    assert second.raw[-1].ended_at == STOPPED.checked_at
    assert [e.status.stream_id for e in ended] == ["b1"]


def test_sitting_that_ended_while_the_service_was_down_ends_with_its_first_live(tmp_path):
    f = tmp_path / "s.json"
    run(f, BEFORE_RESTART)
    second, ended = run(f, [(STOPPED, None)])
    assert second.events == [ev(OFFLINE, "a1")]
    assert second.raw[-1].status.started_at == A.started_at
    assert second.raw[-1].ended_at == STOPPED.checked_at
    assert [e.status.stream_id for e in ended] == ["b1"]


def test_one_live_sitting_that_continues_after_a_restart_ends_with_the_announced_live(tmp_path):
    f = tmp_path / "s.json"
    first, _ = run(f, [(off(), None), (A, off())])  # A announced, still live at the restart
    second, ended = run(f, [(A, None), (B, A), (STOPPED, B)])
    assert first.events == [ev(LIVE, "a1")]
    assert second.events == [ev(OFFLINE, "a1")]
    final = second.raw[-1]
    assert final.status.started_at == A.started_at
    assert final.ended_at == STOPPED.checked_at
    assert [e.status.stream_id for e in ended] == ["a1", "b1"]


ONE_LIVE_IN_GRACE = [(off(), None), (A, off()), (off(), A)]  # stops before the grace period runs out


def test_one_live_sitting_whose_grace_period_ran_at_the_restart_ends_before_the_next_live(tmp_path):
    f = tmp_path / "s.json"
    run(f, ONE_LIVE_IN_GRACE, grace=10)
    second, ended = run(f, [(B, None), (STOPPED, B)])
    assert second.events == [ev(OFFLINE, "a1"), ev(LIVE, "b1"), ev(OFFLINE, "b1")]
    assert second.raw[0].status.started_at == A.started_at
    assert second.raw[0].ended_at == B.checked_at
    assert second.raw[-1].status.started_at == B.started_at
    assert [e.status.stream_id for e in ended] == ["a1", "b1"]


def test_one_live_sitting_whose_grace_period_ran_at_the_restart_continues_once_seen_again(tmp_path):
    f = tmp_path / "s.json"
    run(f, ONE_LIVE_IN_GRACE, grace=10)
    second, ended = run(f, [(A, None), (B, A), (STOPPED, B)])
    assert second.events == [ev(OFFLINE, "a1")]
    assert second.raw[-1].status.started_at == A.started_at
    assert second.raw[-1].ended_at == STOPPED.checked_at
    assert [e.status.stream_id for e in ended] == ["a1", "b1"]


def test_different_live_first_seen_after_a_restart_ends_the_saved_sitting_then_goes_live(tmp_path):
    f = tmp_path / "s.json"
    c = cast("c1", 30, seen=30)
    run(f, BEFORE_RESTART)
    second, ended = run(f, [(c, None)])
    assert second.events == [ev(OFFLINE, "a1"), ev(LIVE, "c1")]
    assert second.raw[0].status.started_at == A.started_at
    assert second.raw[0].ended_at == c.checked_at
    assert [e.status.stream_id for e in ended] == ["b1"]
    third, _ = run(f, [(STOPPED, None)])  # the new sitting is c1 alone
    assert third.events == [ev(OFFLINE, "c1")]
    assert third.raw[-1].status.started_at == c.started_at


# ---- the live-state file ----

def saved(f):
    return json.loads(f.read_text(encoding="utf-8"))


def test_the_sitting_is_one_optional_entry_beside_the_streamers_as_they_were(tmp_path):
    f = tmp_path / "s.json"
    run(f, BEFORE_RESTART)
    raw = saved(f)
    assert raw["version"] == 1
    assert raw["streamers"] == {"youtube:@a": B.to_dict()}  # what a reader without sittings needs
    assert raw["sittings"]["youtube:@a"]["stream_id"] == "a1"
    assert set(raw) == {"version", "streamers", "sittings"}


def test_no_sitting_entry_while_the_sitting_is_its_first_live(tmp_path):
    f = tmp_path / "s.json"
    run(f, [(off(), None), (A, off())])
    assert set(saved(f)) == {"version", "streamers"}


def test_the_sitting_entry_is_removed_when_the_sitting_ends(tmp_path):
    f = tmp_path / "s.json"
    run(f, BEFORE_RESTART + [(STOPPED, B)])
    assert saved(f)["streamers"] == {"youtube:@a": None}
    assert "sittings" not in saved(f)


def test_the_sitting_entry_is_removed_when_a_restart_reports_it_ended(tmp_path):
    f = tmp_path / "s.json"
    run(f, BEFORE_RESTART)
    run(f, [(cast("c1", 30), None)])
    assert saved(f)["streamers"]["youtube:@a"]["stream_id"] == "c1"
    assert "sittings" not in saved(f)


def test_a_file_without_the_sitting_entry_loads_as_before(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"version": 1, "streamers": {"youtube:@a": B.to_dict()}}), encoding="utf-8")
    rec, _ = run(f, [(STOPPED, None)])
    assert rec.events == [ev(OFFLINE, "b1")]
    assert rec.raw[-1].status.started_at == B.started_at


def test_a_sitting_entry_for_a_streamer_that_is_not_live_is_ignored(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"version": 1, "streamers": {"youtube:@a": None},
                             "sittings": {"youtube:@a": A.to_dict()}}), encoding="utf-8")
    rec, _ = run(f, [(B, None)])
    assert rec.events == [ev(LIVE, "b1")]
    assert "sittings" not in saved(f)


@pytest.mark.parametrize("sittings", ['["a list"]', '{"youtube:@a": "text"}', '{"youtube:@a": {"no": "status"}}'])
def test_a_sitting_entry_in_the_wrong_shape_damages_the_file(tmp_path, incidents, sittings):
    f = tmp_path / "s.json"
    f.write_text(f'{{"version": 1, "streamers": {{"youtube:@a": {json.dumps(B.to_dict())}}}, '
                 f'"sittings": {sittings}}}', encoding="utf-8")
    rec, _ = run(f, [(STOPPED, None)])
    assert rec.events == []  # a fresh start: nothing was known
    assert backups(tmp_path, "s.json") and len(incidents) == 1


@pytest.mark.parametrize("with_sitting", [False, True])
def test_the_deploy_check_passes_on_a_live_state_file_with_or_without_the_sitting(
        tmp_path, qq_on, capsys, with_sitting):  # noqa: F811
    cfg = write_config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    raw = {"version": 1, "streamers": {"youtube:@a": B.to_dict()}}
    if with_sitting:
        raw["sittings"] = {"youtube:@a": A.to_dict()}
    (data / "notify_state.json").write_text(json.dumps(raw), encoding="utf-8")
    assert check_state.main(["--config", str(cfg)]) == 0
    assert "状态文件正常" in capsys.readouterr().out


# ---- random sequences with restarts ----

@pytest.mark.slow
def test_random_sequences_with_restarts_end_each_sitting_once_with_the_announced_live(tmp_path):
    """Whatever a channel does and whenever the service restarts, per streamer: notifications
    alternate "went live" and "ended", each "ended" carries the id of the live that was
    announced, the last one is "ended", and every live the notifier saw reaches the listeners
    exactly once. Every live is watched at the channel's address, so sittings continue past
    their first live, before and after a restart."""
    restarts = continued = 0

    async def one(seed: int):
        nonlocal restarts, continued
        rng = random.Random(seed)
        state_file = tmp_path / f"live_state_{seed}.json"
        streamers = [Streamer(f"S{i}", "fake_channel", f"restart{seed}_{i}") for i in range(3)]
        for s in streamers:
            SCRIPT[s.channel] = random_lives(rng, 15, f"{s.channel}-") + [LiveState.OFFLINE] * 3
        rec, ended, seen = Recorder(fail=rng.randint(0, 3)), [], []
        notifier = mon = None
        for check in range(18):
            if check == 0 or (check < 15 and rng.random() < 0.2):
                if notifier:  # a restart: what the next run knows is what the state file holds
                    await notifier.aclose()
                    await mon.aclose()
                    restarts += 1
                notifier = LiveNotifier([rec], notify_on_startup=True, offline_grace=0.01,
                                        retry_base_delay=0.001, state_file=state_file)
                notifier.on_ended.append(ended.append)
                mon = StreamMonitor(streamers)
                mon.on_change.append(notifier.on_change)
                mon.on_change.append(lambda new, old: seen.append(new) if new.is_live else None)
            await mon.refresh()
            if rng.random() < 0.3:
                await asyncio.sleep(0.02)  # sometimes let a grace period run out
        await asyncio.sleep(0.05)
        await notifier.aclose()
        await mon.aclose()
        for s in streamers:
            label = f"seed {seed} {s.name}"
            events = [e for e in rec.events if e[1] == s.id]
            check_invariants(events, label)
            lives = [e.status.stream_id for e in ended if e.status.streamer == s]
            assert sorted(lives) == sorted({st.stream_id for st in seen if st.streamer == s}), label
            assert len(lives) == len(set(lives)), f"{label}: a live reached the listeners twice: {lives}"
            continued += len(lives) - sum(1 for e in events if e[0] is OFFLINE)

    seeds = int(os.getenv("STRESS_SEEDS", "100"))

    async def all_seeds():
        for seed in range(seeds):
            await one(seed)

    asyncio.run(all_seeds())
    if seeds >= 20:  # enough sequences that the service restarted and some lives continued a sitting
        assert restarts > 0 and continued > 0
