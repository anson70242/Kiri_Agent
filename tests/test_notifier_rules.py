"""When go-live / ended events fire (LiveNotifier rules)."""

import asyncio

from backend.notify import LiveNotifier

from .helpers import LIVE, OFFLINE, A, B, Recorder, ev, live, off, unk, up

GRACE = 0.05


async def run_steps(steps, **kw):
    """Feed (new, old) pairs; a number in `steps` means sleep that long."""
    rec = Recorder()
    n = LiveNotifier([rec], offline_grace=kw.pop("grace", GRACE), **kw)
    for step in steps:
        if isinstance(step, (int, float)):
            await asyncio.sleep(step)
        else:
            await n.on_change(*step)
    await asyncio.sleep(GRACE * 3)
    await n.drain()
    await n.aclose()
    return rec.events


def check(steps, expected, **kw):
    assert asyncio.run(run_steps(steps, **kw)) == expected


def test_already_live_at_startup_is_not_announced():
    check([(live(), None)], [])


def test_already_live_at_startup_announced_with_on_startup():
    check([(live(), None)], [ev(LIVE)], notify_on_startup=True)


def test_offline_then_live():
    check([(off(), None), (live(), off())], [ev(LIVE)])


def test_upcoming_then_live():
    check([(up(), None), (live(), up())], [ev(LIVE)])


def test_first_check_failed_then_live_counts_as_startup():
    # UNKNOWN first, then live: we never saw it offline, so it's a startup sighting.
    check([(unk(), None), (live(), unk())], [])


def test_live_then_ended_after_grace():
    check([(off(), None), (live(), off()), (off(), live())], [ev(LIVE), ev(OFFLINE)])


def test_live_then_upcoming_counts_as_ended():
    check([(off(), None), (live(), off()), (up(), live())], [ev(LIVE), ev(OFFLINE)])


def test_ended_reported_for_stream_that_was_live_at_startup():
    check([(live(), None), (off(), live())], [ev(OFFLINE)])


def test_blip_same_stream_back_within_grace_sends_nothing():
    check([(live(), None), (off(), live()), (live(), off())], [], grace=1)


def test_blip_with_missing_stream_id_is_still_same_stream():
    check([(live(), None), (off(), live()), (live(sid=None), off())], [], grace=1)


def test_new_stream_within_grace_reports_ended_then_live_in_order():
    check(
        [(off(), None), (live(sid="v1"), off()), (off(), live()), (live(sid="v2"), off())],
        [ev(LIVE, "v1"), ev(OFFLINE, "v1"), ev(LIVE, "v2")],
        grace=1,
    )


def test_live_to_live_new_stream_between_polls():
    check(
        [(off(), None), (live(sid="v1"), off()), (live(sid="v2"), live(sid="v1"))],
        [ev(LIVE, "v1"), ev(OFFLINE, "v1"), ev(LIVE, "v2")],
    )


def test_missing_stream_id_never_looks_like_a_new_stream():
    check([(off(), None), (live(sid="v1"), off()), (live(sid=None), live(sid="v1"))], [ev(LIVE, "v1")])


def test_several_offline_states_during_grace_send_one_ended():
    check(
        [(off(), None), (live(), off()), (off(), live()), (up(), off()), (off(), up())],
        [ev(LIVE), ev(OFFLINE)],
    )


def test_unknown_is_ignored():
    check([(off(), None), (live(), off()), (unk(), live()), (live(), unk())], [ev(LIVE)])


def test_on_offline_false_sends_only_live():
    check([(off(), None), (live(), off()), (off(), live())], [ev(LIVE)], notify_offline=False)


def test_streamers_are_independent():
    check(
        [(off(A), None), (off(B), None), (live(A), off(A)), (live(B, sid="b1"), off(B)),
         (off(A), live(A))],
        [ev(LIVE, "v1", A), ev(LIVE, "b1", B), ev(OFFLINE, "v1", A)],
    )


def test_full_day():
    check(
        [
            (off(), None),
            (live(sid="v1"), off()),
            (off(), live(sid="v1")), GRACE * 3,  # really ended
            (up(), off()),
            (live(sid="v2"), up()),
            (off(), live(sid="v2")),  # blip
            (live(sid="v2"), off()),
            (live(sid="v3"), live(sid="v2")),  # restarted between polls
            (off(), live(sid="v3")),
        ],
        [ev(LIVE, "v1"), ev(OFFLINE, "v1"), ev(LIVE, "v2"), ev(OFFLINE, "v2"), ev(LIVE, "v3"), ev(OFFLINE, "v3")],
        grace=GRACE,
    )


def test_ended_event_carries_last_live_details_and_end_time():
    async def go():
        rec = Recorder()
        n = LiveNotifier([rec], offline_grace=0)
        await n.on_change(off(), None)
        await n.on_change(live(), off())
        ended = off()
        await n.on_change(ended, live())
        await asyncio.sleep(0.05)
        await n.aclose()
        offline_event = rec.raw[-1]
        assert offline_event.status.title == "title v1"
        assert offline_event.status.url == "https://x/v1"
        assert offline_event.ended_at == ended.checked_at

    asyncio.run(go())
