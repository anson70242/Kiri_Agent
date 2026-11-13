"""The live's cover on pipeline messages: QQ ("回放已下载", "全部处理完成") and Discord (every one)."""

from __future__ import annotations

import httpx
import pytest

from backend.notify import EventKind, StreamEvent

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
