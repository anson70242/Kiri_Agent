"""The pipeline tells the source record sheet when "全部处理完成" goes out: the streamer, the
archive's date, the live's title, the 全部下载 link the message carries and when it expires,
and the stream id. Values, not a message; nothing about groups or sheets. A recorder stands
in for the module. Through the automatic download with a fake clock and a fake drive."""

from __future__ import annotations

from backend.downloader.auto import ShortDownloadLink
from backend.notify import EventKind

from .helpers import A, B
from .test_drive_stage import SITE, FakeDrive, downloaded, kinds, make, rewrite_queue, step

EXPIRES = 1_797_957_000.0


class Recorder:
    """Notes every telling as (what, streamer, values)."""

    def __init__(self, error: Exception | None = None) -> None:
        self.told: list[tuple[str, object, dict]] = []
        self.error = error

    def processed(self, streamer, **values) -> None:
        self.told.append(("processed", streamer, values))
        if self.error:
            raise self.error

    def uploaded(self, streamer, **values) -> None:
        self.told.append(("uploaded", streamer, values))
        if self.error:
            raise self.error


def recorded(tmp_path, drive=None, **kw):
    ad, clock, sent = make(tmp_path, drive, **kw)
    ad.source_record = Recorder()
    return ad, clock, sent, ad.source_record


def ready(sent):
    (event,) = [e for e in sent if e.kind is EventKind.READY]
    return event


def test_ready_tells_the_streamer_the_date_the_title_the_messages_link_and_the_stream_id(tmp_path):
    ad, _, sent, record = recorded(tmp_path, folders={})
    downloaded(ad)
    step(ad, "transcribe")

    assert kinds(sent)[-1] is EventKind.READY
    assert record.told == [("processed", A, {
        "date": "20261110", "title": "title v1", "link": ready(sent).links[0].url, "expires": None,
        "stream_id": "v1"})]


def test_the_link_told_is_the_short_download_link_of_the_message_with_its_expiry(tmp_path):
    ad, _, sent, record = recorded(tmp_path, folders={})
    made: list[str] = []

    def short(folder: str) -> ShortDownloadLink:
        made.append(folder)
        return ShortDownloadLink(f"/d/Code{len(made):06d}", EXPIRES)

    ad.download_link = short
    downloaded(ad)
    step(ad, "transcribe")

    assert made == ["A/20261110/歌枠"]  # one short download link: the message's
    ((_, _, values),) = record.told
    assert values["link"] == ready(sent).links[0].url == f"{SITE}/d/Code000001"
    assert values["expires"] == EXPIRES


def test_without_a_public_address_the_message_has_no_link_and_nothing_is_told(tmp_path):
    ad, _, sent, record = recorded(tmp_path, folders={})
    ad.site_url = ""
    downloaded(ad)
    step(ad, "transcribe")

    assert ready(sent).links == ()
    assert record.told == []


def test_a_live_with_a_drive_job_is_told_processed_once_when_ready_goes_out_not_again_at_the_upload(tmp_path):
    ad, _, sent, record = recorded(tmp_path, FakeDrive())
    downloaded(ad)
    step(ad, "transcribe")
    assert [what for what, _, _ in record.told] == ["processed"]
    assert record.told[0][2]["link"] == ready(sent).links[0].url

    step(ad, "drive")
    assert kinds(sent)[-1] is EventKind.DRIVE_UPLOADED
    assert [what for what, _, _ in record.told] == ["processed", "uploaded"]


def test_a_drive_job_whose_ready_went_out_only_with_its_drive_message_is_told_then(tmp_path):
    ad, _, _, _ = recorded(tmp_path, FakeDrive())
    downloaded(ad)
    step(ad, "transcribe")
    rewrite_queue(tmp_path, lambda job: job.update(ready_sent=False))
    restarted, _, sent, record = recorded(tmp_path, FakeDrive())

    step(restarted, "drive")
    assert kinds(sent) == [EventKind.READY, EventKind.DRIVE_UPLOADED]
    assert [what for what, _, _ in record.told] == ["processed", "uploaded"]
    assert record.told[:1] == [("processed", A, {
        "date": "20261110", "title": "title v1", "link": ready(sent).links[0].url, "expires": None,
        "stream_id": "v1"})]


def test_each_live_is_told_with_its_own_streamer(tmp_path):
    ad, _, _, record = recorded(tmp_path, folders={})
    downloaded(ad, B)
    step(ad, "transcribe")
    assert [streamer for _, streamer, _ in record.told] == [B]


def test_a_failing_record_holds_up_neither_the_messages_nor_the_drive_job(tmp_path):
    ad, _, sent, _ = recorded(tmp_path, FakeDrive())
    ad.source_record = Recorder(RuntimeError("the sheet is broken"))
    downloaded(ad)
    step(ad, "transcribe")

    assert kinds(sent)[-1] is EventKind.READY
    assert [j.stage for j in ad.load()] == ["drive"]
    step(ad, "drive")
    assert kinds(sent)[-1] is EventKind.DRIVE_UPLOADED
