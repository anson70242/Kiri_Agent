"""When "scheduled a live" is announced (LiveNotifier rules for an upcoming live)."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from backend.notify import EventKind, LiveNotifier

from .helpers import LIVE, OFFLINE, T0, Recorder, ev, live, off, up

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


def test_the_next_live_scheduled_as_one_ends_is_announced_after_the_end():
    # The waiting room shows the moment the live stops; "ended" still waits out its grace
    # period, and the schedule is not announced ahead of it.
    check([(off(), None), (live(sid="v1"), off()), (up(sid="u2"), live(sid="v1"))],
          [ev(LIVE, "v1"), ev(OFFLINE, "v1"), ev(UPCOMING, "u2")])


def test_a_waiting_room_seen_during_a_dropout_is_announced_when_that_live_really_ends():
    check([(off(), None), (live(sid="v1"), off()), (up(sid="u2"), live(sid="v1")), (live(sid="v1"), up(sid="u2")),
           (off(), live(sid="v1"))],
          [ev(LIVE, "v1"), ev(OFFLINE, "v1"), ev(UPCOMING, "u2")])


def test_a_waiting_room_that_goes_live_right_after_the_last_live_is_not_announced_as_scheduled():
    # Seen as the last live stopped, on air by the next check: "went live" says it all.
    check([(off(), None), (live(sid="v1"), off()), (up(sid="u2"), live(sid="v1")), (live(sid="u2"), up(sid="u2"))],
          [ev(LIVE, "v1"), ev(OFFLINE, "v1"), ev(LIVE, "u2")])


# ---- whenever it is scheduled for ----

def scheduled_in(days: float, sid: str = "u1"):
    """An upcoming live checked at T0 and scheduled `days` later."""
    return replace(up(sid=sid, starts=T0 + timedelta(days=days)), checked_at=T0)


def test_a_waiting_room_scheduled_a_year_ahead_is_announced_once_like_any_other():
    # A permanent "news" or free-chat room shows whenever the streamer is not live: news
    # the first time, never again (the owner's choice, 2026-11-16).
    far = scheduled_in(343)
    check([(off(), None), (far, off()), (off(), far), (far, off())], [ev(UPCOMING, "u1")])
    check([(off(), None), (live(sid="v1"), off()), (far, live(sid="v1")), (off(), far), (far, off())],
          [ev(LIVE, "v1"), ev(OFFLINE, "v1"), ev(UPCOMING, "u1")])


def test_a_live_that_ends_back_into_a_waiting_room_already_announced_says_only_ended():
    # The permanent room was announced when it first showed. Every live after that ends
    # with the state going from live straight back to that room: "ended", nothing else.
    room = scheduled_in(343, sid="room")
    check([(off(), None), (room, off()),
           (live(sid="v1"), room), (room, live(sid="v1")),
           (live(sid="v2"), room), (room, live(sid="v2"))],
          [ev(UPCOMING, "room"), ev(LIVE, "v1"), ev(OFFLINE, "v1"), ev(LIVE, "v2"), ev(OFFLINE, "v2")])


def test_a_permanent_waiting_room_is_not_forgotten_however_many_lives_are_scheduled_after_it(tmp_path):
    # Only so many announced rooms are remembered per streamer. The permanent one shows
    # again after every live, so it must stay remembered: never announced a second time.
    room = up(sid="room")
    steps = [(off(), None), (room, off())]
    for i in range(30):
        steps += [(up(sid=f"u{i}"), room), (room, up(sid=f"u{i}"))]
    f = tmp_path / "s.json"

    events = asyncio.run(session(steps, f))
    after_restart = asyncio.run(session([(room, None), (off(), room), (room, off())], f))

    assert events == [ev(UPCOMING, "room")] + [ev(UPCOMING, f"u{i}") for i in range(30)]
    assert after_restart == []


def test_a_scheduled_live_whose_time_has_passed_or_is_unknown_is_still_announced():
    check([(off(), None), (scheduled_in(-0.01), off())], [ev(UPCOMING, "u1")])  # waiting room open, running late
    unknown = replace(up(sid="u1"), started_at=None)
    check([(off(), None), (unknown, off())], [ev(UPCOMING, "u1")])


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
