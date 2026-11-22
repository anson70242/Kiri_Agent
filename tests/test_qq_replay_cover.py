"""The replay's cover in QQ: a message whose cover address differs from the one the live's kept
cover was fetched from fetches the new picture (a Twitch live preview turns into a camera once
the live ended, so 「回放已下载」 carries the replay's cover instead)."""

from __future__ import annotations

import httpx
import pytest

from backend.notify import EventKind, StreamEvent
from backend.notify.qq import COVER_KINDS

from .helpers import A, live, up
from .test_pipeline_covers import ALL_KINDS, RefusesReferences
from .test_qq import JPEG, SUBSCRIBED, notifier, send

# Made-up addresses, shaped like Twitch's.
PREVIEW = "https://cdn.example/previews/live_user_a-640x360.jpg?t=1"
REPLAY_COVER = "https://cdn.example/vods/a1b2c3/v1/thumb/thumb0-640x360.jpg"


class ReplayQQ(RefusesReferences):
    """FakeQQ that also serves the live preview and the replay's cover, counting each fetch;
    `uploads_fail` and `refused` as in RefusesReferences."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.ok = {PREVIEW: True, REPLAY_COVER: True}
        self.fetched: dict[str, int] = {PREVIEW: 0, REPLAY_COVER: 0}

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in self.ok:
            self.fetched[url] += 1
            return httpx.Response(200, content=JPEG) if self.ok[url] else httpx.Response(404)
        return super().handle(request)


def qq(tmp_path, fake):
    return notifier(tmp_path, fake, {**SUBSCRIBED, "kinds": ALL_KINDS})


def event(kind: EventKind, cover: str | None) -> StreamEvent:
    return StreamEvent(kind, live(A, thumbnail=cover), detail="x")


def test_downloaded_with_the_replays_cover_fetches_it_once_and_ready_reuses_that_upload(tmp_path):
    fake = ReplayQQ()
    n = qq(tmp_path, fake)
    send(n, event(EventKind.LIVE, PREVIEW))
    send(n, event(EventKind.DOWNLOADED, REPLAY_COVER))
    send(n, event(EventKind.READY, REPLAY_COVER))
    went_live, downloaded, ready = fake.texts("G1")
    assert "COVER1)" in went_live
    assert "COVER2)" in downloaded and "COVER2)" in ready
    assert fake.fetched == {PREVIEW: 1, REPLAY_COVER: 1} and fake.uploads == 2


def cannot_use_the_replay_cover(fake: ReplayQQ, failing: str) -> None:
    if failing == "fetch":
        fake.ok[REPLAY_COVER] = False
    else:
        fake.uploads_fail = True


@pytest.mark.parametrize("failing", ["fetch", "upload"])
def test_a_replay_cover_that_cannot_be_used_leaves_the_cover_kept_for_the_live(tmp_path, failing):
    fake = ReplayQQ()
    n = qq(tmp_path, fake)
    send(n, event(EventKind.LIVE, PREVIEW))
    cannot_use_the_replay_cover(fake, failing)
    send(n, event(EventKind.DOWNLOADED, REPLAY_COVER))
    send(n, event(EventKind.READY, REPLAY_COVER))
    _, downloaded, ready = fake.texts("G1")
    assert "COVER1)" in downloaded and "COVER1)" in ready
    assert fake.uploads == 1


@pytest.mark.parametrize("failing", ["fetch", "upload"])
def test_a_replay_cover_that_cannot_be_used_with_none_kept_leaves_the_text(tmp_path, failing):
    fake = ReplayQQ()
    cannot_use_the_replay_cover(fake, failing)
    send(qq(tmp_path, fake), event(EventKind.DOWNLOADED, REPLAY_COVER))
    (text,) = fake.texts("G1")
    assert text.startswith("📥 **A** 回放已下载\ntitle v1\n") and "![" not in text


def test_a_replay_cover_that_failed_is_tried_again_by_the_next_message(tmp_path):
    """Twitch may be slow for 「回放已下载」 and answer by 「全部处理完成」."""
    fake = ReplayQQ()
    n = qq(tmp_path, fake)
    send(n, event(EventKind.LIVE, PREVIEW))
    fake.ok[REPLAY_COVER] = False
    send(n, event(EventKind.DOWNLOADED, REPLAY_COVER))
    fake.ok[REPLAY_COVER] = True
    send(n, event(EventKind.READY, REPLAY_COVER))
    _, downloaded, ready = fake.texts("G1")
    assert "COVER1)" in downloaded and "COVER2)" in ready


def test_after_a_restart_ready_fetches_the_cover_from_the_address_it_carries(tmp_path):
    fake = ReplayQQ()
    send(qq(tmp_path, fake), event(EventKind.LIVE, PREVIEW))
    restarted = qq(tmp_path, fake)  # nothing kept
    send(restarted, event(EventKind.READY, REPLAY_COVER))
    assert "COVER2)" in fake.texts("G1")[1]
    assert fake.fetched == {PREVIEW: 1, REPLAY_COVER: 1}


def test_a_live_whose_cover_address_did_not_change_reuses_its_upload(tmp_path):
    fake = ReplayQQ()
    n = qq(tmp_path, fake)
    for kind in (EventKind.LIVE, EventKind.DOWNLOADED, EventKind.READY):
        send(n, event(kind, PREVIEW))
    assert all("COVER1)" in text for text in fake.texts("G1"))
    assert fake.fetched[PREVIEW] == 1 and fake.uploads == 1


def test_a_message_without_a_cover_address_uses_the_cover_kept_for_the_live(tmp_path):
    """No replay cover and no avatar: the picture kept from 「开播了」 rather than none."""
    fake = ReplayQQ()
    n = qq(tmp_path, fake)
    send(n, event(EventKind.LIVE, PREVIEW))
    send(n, event(EventKind.DOWNLOADED, None))
    assert "COVER1)" in fake.texts("G1")[1]
    assert fake.fetched[PREVIEW] == 1 and fake.uploads == 1


def test_going_live_after_the_announcement_still_fetches_the_cover_again(tmp_path):
    fake = ReplayQQ()
    n = qq(tmp_path, fake)
    status = up(A, "v1")
    status.thumbnail = PREVIEW
    send(n, StreamEvent(EventKind.UPCOMING, status))
    send(n, event(EventKind.LIVE, PREVIEW))  # the same address
    send(n, event(EventKind.DOWNLOADED, PREVIEW))
    announced, went_live, downloaded = fake.texts("G1")
    assert "COVER1)" in announced and "COVER2)" in went_live and "COVER2)" in downloaded
    assert fake.fetched[PREVIEW] == 2


@pytest.mark.parametrize("kind", sorted(set(EventKind) - COVER_KINDS))
def test_the_messages_without_a_cover_stay_text_with_the_replays_cover(tmp_path, kind):
    fake = ReplayQQ()
    n = qq(tmp_path, fake)
    send(n, event(EventKind.LIVE, PREVIEW))
    send(n, event(kind, REPLAY_COVER))
    assert "![" not in fake.texts("G1")[1]
    assert fake.fetched[REPLAY_COVER] == 0 and fake.uploads == 1


def test_a_refused_replay_cover_uploaded_again_is_still_known_by_its_address(tmp_path):
    fake = ReplayQQ()
    n = qq(tmp_path, fake)
    send(n, event(EventKind.LIVE, PREVIEW))
    send(n, event(EventKind.DOWNLOADED, REPLAY_COVER))
    fake.refused.add("qqbot-media://image/v1/COVER2")
    send(n, event(EventKind.READY, REPLAY_COVER))  # refused, uploaded again from the kept image
    send(n, event(EventKind.READY, REPLAY_COVER))  # the next message of the live
    assert "COVER3)" in fake.texts("G1")[-1]
    assert fake.fetched[REPLAY_COVER] == 1 and fake.uploads == 3
