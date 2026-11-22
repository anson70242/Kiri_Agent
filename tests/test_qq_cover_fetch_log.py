"""A cover the QQ notifier cannot fetch is logged by the kind of failure, never by its address."""

from __future__ import annotations

import logging

from backend.notify import EventKind

from .test_qq_replay_cover import REPLAY_COVER, ReplayQQ, event, qq
from .test_qq import send


def test_a_cover_that_cannot_be_fetched_is_logged_without_its_address(tmp_path, caplog):
    fake = ReplayQQ()
    fake.ok[REPLAY_COVER] = False  # answered with 404
    with caplog.at_level(logging.WARNING, logger="backend.notify.qq"):
        send(qq(tmp_path, fake), event(EventKind.DOWNLOADED, REPLAY_COVER))

    assert "qq: cover not used (HTTPStatusError, status 404)" in caplog.messages
    assert REPLAY_COVER not in caplog.text
