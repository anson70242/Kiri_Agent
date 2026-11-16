"""When "scheduled a live" is announced (LiveNotifier rules for an upcoming live)."""

import asyncio

from backend.notify import EventKind, LiveNotifier

from .helpers import LIVE, OFFLINE, Recorder, ev, live, off, up

UPCOMING = EventKind.UPCOMING
GRACE = 0.05


async def session(steps, state_file=None, **kw):
    """One monitor run: feed (new, old) pairs to a fresh LiveNotifier."""
    rec = Recorder()
    n = LiveNotifier([rec], offline_grace=GRACE, state_file=state_file, **kw)
    for step in steps:
        await n.on_change(*step)
    await asyncio.sleep(GRACE * 3)
    await n.drain()
    await n.aclose()
    return rec.events


def check(steps, expected, **kw):
    assert asyncio.run(session(steps, **kw)) == expected


def test_a_streamer_who_schedules_a_live_is_announced():
    check([(off(), None), (up(sid="u1"), off())], [ev(UPCOMING, "u1")])


def test_a_scheduled_live_is_announced_once():
    # Seen again after a failed check, and again when YouTube shows another scheduled live
    # in between (a channel with two waiting rooms flips between them).
    check([(off(), None), (up(sid="u1"), off()), (off(), up(sid="u1")), (up(sid="u1"), off()),
           (up(sid="u2"), up(sid="u1")), (up(sid="u1"), up(sid="u2")), (up(sid="u2"), up(sid="u1"))],
          [ev(UPCOMING, "u1"), ev(UPCOMING, "u2")])


def test_going_live_is_still_announced_after_the_schedule_was():
    check([(off(), None), (up(sid="v1"), off()), (live(sid="v1"), up(sid="v1"))], [ev(UPCOMING, "v1"), ev(LIVE, "v1")])


def test_the_next_live_scheduled_right_after_one_ends():
    check([(off(), None), (live(sid="v1"), off()), (up(sid="u2"), live(sid="v1"))],
          [ev(LIVE, "v1"), ev(UPCOMING, "u2"), ev(OFFLINE, "v1")])


def test_an_upcoming_live_without_an_id_is_never_announced():
    check([(off(), None), (up(), off())], [])


# ---- startup and restarts ----

def test_a_live_already_scheduled_at_the_first_start_is_not_announced():
    check([(up(sid="u1"), None)], [])
    check([(up(sid="u1"), None)], [ev(UPCOMING, "u1")], notify_on_startup=True)


def test_a_scheduled_live_is_not_announced_again_after_a_restart(tmp_path):
    f = tmp_path / "s.json"
    first = asyncio.run(session([(off(), None), (up(sid="u1"), off())], f))
    second = asyncio.run(session([(up(sid="u1"), None), (off(), up(sid="u1")), (up(sid="u1"), off())], f))
    assert (first, second) == ([ev(UPCOMING, "u1")], [])


def test_a_live_scheduled_while_the_service_was_down_is_announced_after_the_restart(tmp_path):
    f = tmp_path / "s.json"
    asyncio.run(session([(off(), None), (up(sid="u1"), off())], f))
    assert asyncio.run(session([(up(sid="u2"), None)], f)) == [ev(UPCOMING, "u2")]


def test_the_first_start_with_this_rule_does_not_announce_what_is_already_scheduled(tmp_path):
    # A state file from before upcoming lives were announced: it knows the streamer, and
    # nothing about what was scheduled. What is scheduled right now is old news.
    f = tmp_path / "s.json"
    asyncio.run(session([(off(), None), (live(sid="v1"), off()), (off(), live(sid="v1"))], f))
    assert "upcoming" not in f.read_text(encoding="utf-8")
    second = asyncio.run(session([(up(sid="u1"), None), (up(sid="u2"), up(sid="u1"))], f))
    assert second == [ev(UPCOMING, "u2")]  # u1 was there at the start; u2 is new
