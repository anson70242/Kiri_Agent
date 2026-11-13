"""The live's cover on pipeline messages: QQ ("回放已下载", "全部处理完成") and Discord (every one)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.notify import EventKind, StreamEvent
from backend.notify.discord import DiscordNotifier
from backend.notify.qq import COVERS_KEPT

from .helpers import A, live
from .test_qq import COVER, SUBSCRIBED, FakeQQ, notifier, send

ALL_KINDS = [str(k) for k in EventKind]


class CoverQQ(FakeQQ):
    """FakeQQ that counts cover fetches and gives every upload its own reference."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.fetches = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(COVER):
            self.fetches += 1
        if request.url.path == "/v2/media-images/complete":
            return httpx.Response(200, json={"image_id": f"I{self.uploads}",
                                             "url": f"qqbot-media://image/v1/COVER{self.uploads}"})
        return super().handle(request)


def qq(tmp_path, fake):
    return notifier(tmp_path, fake, {**SUBSCRIBED, "kinds": ALL_KINDS})


@pytest.mark.parametrize("kind, headline", [
    (EventKind.DOWNLOADED, "📥 **A** 回放已下载"),
    (EventKind.READY, "✅ **A** 全部处理完成"),
])
def test_downloaded_and_ready_carry_the_cover_after_the_headline(tmp_path, kind, headline):
    fake = CoverQQ()
    send(qq(tmp_path, fake), StreamEvent(kind, live(A, thumbnail=COVER), detail="6.2 GB"))
    lines = fake.texts("G1")[0].split("\n")
    assert lines[0] == headline
    assert lines[1].startswith("![封面 #") and lines[1].endswith("(qqbot-media://image/v1/COVER1)")
    assert lines[2] == "title v1"


@pytest.mark.parametrize("kind", sorted(set(EventKind) - {EventKind.LIVE, EventKind.DOWNLOADED, EventKind.READY}))
def test_the_other_messages_stay_text_only(tmp_path, kind):
    fake = CoverQQ()
    send(qq(tmp_path, fake), StreamEvent(kind, live(A, thumbnail=COVER), detail="x"))
    (text,) = fake.texts("G1")
    assert "![" not in text
    assert fake.fetches == 0 and fake.uploads == 0


def test_one_upload_serves_every_message_of_a_live(tmp_path):
    fake = CoverQQ()
    n = qq(tmp_path, fake)
    for kind in (EventKind.LIVE, EventKind.DOWNLOADED, EventKind.READY):
        send(n, StreamEvent(kind, live(A, thumbnail=COVER), detail="x"))
    assert fake.fetches == 1 and fake.uploads == 1
    assert all("(qqbot-media://image/v1/COVER1)" in text for text in fake.texts("G1"))


def test_the_stale_cover_address_is_not_fetched_again(tmp_path):
    """The live's address may show another picture by the time the archive is downloaded."""
    fake = CoverQQ()
    n = qq(tmp_path, fake)
    send(n, StreamEvent(EventKind.LIVE, live(A, thumbnail=COVER)))
    fake.cover_ok = False  # the address now serves nothing of this live
    send(n, StreamEvent(EventKind.DOWNLOADED, live(A, thumbnail=COVER), detail="x"))
    assert "(qqbot-media://image/v1/COVER1)" in fake.texts("G1")[1]


def test_another_live_gets_its_own_cover(tmp_path):
    fake = CoverQQ()
    n = qq(tmp_path, fake)
    send(n, StreamEvent(EventKind.LIVE, live(A, thumbnail=COVER)))
    send(n, StreamEvent(EventKind.LIVE, live(A, sid="v2", thumbnail=COVER)))  # same address, another live
    send(n, StreamEvent(EventKind.DOWNLOADED, live(A, thumbnail=COVER), detail="x"))
    first, second, downloaded = fake.texts("G1")
    assert "COVER1)" in first and "COVER2)" in second and "COVER1)" in downloaded
    assert fake.uploads == 2


def test_only_the_latest_lives_keep_their_uploaded_cover(tmp_path):
    fake = CoverQQ()
    n = qq(tmp_path, fake)
    for i in range(COVERS_KEPT + 1):
        send(n, StreamEvent(EventKind.LIVE, live(A, sid=f"v{i}", thumbnail=COVER)))
    send(n, StreamEvent(EventKind.DOWNLOADED, live(A, sid="v1", thumbnail=COVER), detail="x"))
    assert fake.fetches == COVERS_KEPT + 1  # still kept
    send(n, StreamEvent(EventKind.DOWNLOADED, live(A, sid="v0", thumbnail=COVER), detail="x"))
    assert fake.fetches == COVERS_KEPT + 2  # forgotten: fetched again


def test_a_live_without_a_stream_id_fetches_its_cover_each_time(tmp_path):
    fake = CoverQQ()
    n = qq(tmp_path, fake)
    for kind in (EventKind.LIVE, EventKind.DOWNLOADED):
        send(n, StreamEvent(kind, live(A, sid=None, thumbnail=COVER), detail="x"))
    assert fake.fetches == 2


class RefusesReferences(CoverQQ):
    """Refuses messages that carry one of `refused` (a reference QQ won't take again)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.refused: set[str] = set()
        self.refusals = 0
        self.uploads_fail = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.uploads_fail and request.url.path == "/v2/media-images/prepare":
            return httpx.Response(500, json={"code": 500000, "message": "server error"})
        if request.url.path.endswith("/messages") and any(r in request.content.decode() for r in self.refused):
            self.refusals += 1
            return httpx.Response(400, json={"code": 40034999, "message": "图片资源无效"})
        return super().handle(request)


TWO_GROUPS = ({**SUBSCRIBED, "kinds": ALL_KINDS}, {**SUBSCRIBED, "openid": "G2", "kinds": ALL_KINDS})


def test_a_refused_reference_is_uploaded_again_from_the_kept_image(tmp_path):
    fake = RefusesReferences()
    n = notifier(tmp_path, fake, *TWO_GROUPS)
    send(n, StreamEvent(EventKind.LIVE, live(A, thumbnail=COVER)))
    fake.refused.add("qqbot-media://image/v1/COVER1")
    fake.cover_ok = False  # the address went stale: the kept image is what is uploaded
    send(n, StreamEvent(EventKind.DOWNLOADED, live(A, thumbnail=COVER), detail="x"))
    send(n, StreamEvent(EventKind.READY, live(A, thumbnail=COVER), detail="x"))

    for group in ("G1", "G2"):
        _, downloaded, ready = fake.texts(group)
        assert "(qqbot-media://image/v1/COVER2)" in downloaded and "(qqbot-media://image/v1/COVER2)" in ready
    assert fake.uploads == 2 and fake.refusals == 1
    assert all(e["ok"] for e in n.history.entries())  # the refused try is not a failed delivery


def test_a_refused_reference_that_cannot_be_uploaded_again_leaves_the_text(tmp_path):
    fake = RefusesReferences()
    n = qq(tmp_path, fake)
    send(n, StreamEvent(EventKind.LIVE, live(A, thumbnail=COVER)))
    fake.refused.add("qqbot-media://image/v1/COVER1")
    fake.uploads_fail = True
    send(n, StreamEvent(EventKind.DOWNLOADED, live(A, thumbnail=COVER), detail="x"))
    downloaded = fake.texts("G1")[1]
    assert downloaded.startswith("📥 **A** 回放已下载\ntitle v1\n") and "![" not in downloaded


# ---- Discord ----

def discord_embed(event: StreamEvent) -> dict:
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(204)

    n = DiscordNotifier("https://discord.test/hook", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(n.send(event))
    (payload,) = sent
    return payload["embeds"][0]


PIPELINE_KINDS = sorted(set(EventKind) - {EventKind.LIVE, EventKind.OFFLINE})


@pytest.mark.parametrize("kind", PIPELINE_KINDS)
def test_every_discord_pipeline_message_shows_the_cover_as_a_thumbnail(kind):
    embed = discord_embed(StreamEvent(kind, live(A, thumbnail="https://cdn/cover.jpg"), detail="x"))
    assert embed["thumbnail"] == {"url": "https://cdn/cover.jpg"}
    assert "image" not in embed


@pytest.mark.parametrize("kind", PIPELINE_KINDS)
def test_a_discord_pipeline_message_without_a_cover_has_no_thumbnail(kind):
    assert "thumbnail" not in discord_embed(StreamEvent(kind, live(A), detail="x"))


class UploadRefused(CoverQQ):
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/media-images/prepare":
            return httpx.Response(500, json={"code": 500000, "message": "server error"})
        return super().handle(request)


@pytest.mark.parametrize("fake", [CoverQQ(cover_ok=False), UploadRefused()], ids=["fetch fails", "upload fails"])
def test_without_a_cover_the_pipeline_message_still_goes_out_as_text(tmp_path, fake):
    send(qq(tmp_path, fake), StreamEvent(EventKind.DOWNLOADED, live(A, thumbnail=COVER), detail="6.2 GB"))
    (text,) = fake.texts("G1")
    assert text.startswith("📥 **A** 回放已下载\ntitle v1\n") and "![" not in text
