"""A sitting survives a restart: after the service restarts in the middle of a sitting that
already continued past its first live, the one "ended" still covers the whole sitting."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from backend import check_state
from backend.notify import LiveNotifier

from .helpers import LIVE, OFFLINE, T0, Recorder, ev, off
from .test_check_state import qq_on, write_config  # noqa: F401  (qq_on is a fixture)
from .test_live_continues import cast
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
