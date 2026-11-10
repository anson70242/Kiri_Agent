"""State survives restarts: nothing lost or duplicated when the monitor restarts."""

import asyncio
import json

from backend.notify import LiveNotifier

from .helpers import LIVE, OFFLINE, Recorder, ev, live, off

GRACE = 0.05


async def session(state_file, steps, grace=GRACE, settle=True):
    """One monitor 'run': a fresh LiveNotifier on the same state file."""
    rec = Recorder()
    n = LiveNotifier([rec], offline_grace=grace, state_file=state_file)
    for new, old in steps:
        await n.on_change(new, old)
    if settle:
        await asyncio.sleep(grace * 3)
    await n.drain()
    await n.aclose()
    return rec.events


def run(*sessions):
    return [asyncio.run(s) for s in sessions]


def test_restart_while_same_stream_still_live_sends_nothing(tmp_path):
    f = tmp_path / "s.json"
    first, second = run(
        session(f, [(off(), None), (live(), off())]),
        session(f, [(live(), None)]),
    )
    assert first == [ev(LIVE)]
    assert second == []


def test_stream_ended_while_down_is_reported_after_restart(tmp_path):
    f = tmp_path / "s.json"
    _, second = run(
        session(f, [(off(), None), (live(), off())]),
        session(f, [(off(), None)]),
    )
    assert second == [ev(OFFLINE)]


def test_new_stream_started_while_down(tmp_path):
    f = tmp_path / "s.json"
    _, second = run(
        session(f, [(off(), None), (live(sid="v1"), off())]),
        session(f, [(live(sid="v2"), None)]),
    )
    assert second == [ev(OFFLINE, "v1"), ev(LIVE, "v2")]


def test_went_live_while_down_is_reported_after_restart(tmp_path):
    f = tmp_path / "s.json"
    _, second = run(
        session(f, [(off(), None)]),
        session(f, [(live(), None)]),
    )
    assert second == [ev(LIVE)]


def test_restart_inside_grace_window_still_reports_ended(tmp_path):
    f = tmp_path / "s.json"
    first, second = run(
        # Stops 0s after going offline: grace never finished.
        session(f, [(off(), None), (live(), off()), (off(), live())], grace=10, settle=False),
        session(f, [(off(), None)]),
    )
    assert first == [ev(LIVE)]
    assert second == [ev(OFFLINE)]


def test_ended_not_repeated_on_later_restarts(tmp_path):
    f = tmp_path / "s.json"
    results = run(
        session(f, [(off(), None), (live(), off()), (off(), live())]),
        session(f, [(off(), None)]),
        session(f, [(off(), None)]),
    )
    assert results == [[ev(LIVE), ev(OFFLINE)], [], []]


def test_corrupt_state_file_starts_fresh(tmp_path):
    f = tmp_path / "s.json"
    f.write_text("{not json", encoding="utf-8")
    (events,) = run(session(f, [(live(), None)]))
    assert events == []  # treated as first sighting, no crash
    assert json.loads(f.read_text(encoding="utf-8"))["streamers"]  # rewritten cleanly


def test_state_file_contents(tmp_path):
    f = tmp_path / "sub" / "s.json"
    run(session(f, [(off(), None), (live(), off())]))
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["streamers"]["youtube:@a"]["stream_id"] == "v1"
    assert not (tmp_path / "sub" / "s.tmp").exists()
