"""The pipeline tells the source record sheet when "网盘上传完成" goes out with a drive link:
the streamer, the archive's date, the live's title, the drive link the message carries (its
extraction code inside) and the stream id. A recorder stands in for the module. Through the
automatic download with a fake clock and a fake drive."""

from __future__ import annotations

from backend.drive import DriveCopy
from backend.notify import EventKind

from .helpers import A, B
from .test_drive_stage import FakeDrive, downloaded, kinds, step
from .test_source_record_pipeline import Recorder, recorded

DRIVE_LINK = "https://drive.example/s/MadeUp42?pwd=q7w8"


def told(record, what):
    return [(streamer, values) for w, streamer, values in record.told if w == what]


def drive_uploaded(sent):
    (event,) = [e for e in sent if e.kind is EventKind.DRIVE_UPLOADED]
    return event


def test_an_upload_with_a_drive_link_tells_the_streamer_the_date_the_title_the_messages_link_and_the_stream_id(
        tmp_path):
    ad, _, sent, record = recorded(tmp_path, FakeDrive(DriveCopy("录播/A/v.zip", DRIVE_LINK)))
    downloaded(ad)
    step(ad, "transcribe")
    step(ad, "drive")

    assert drive_uploaded(sent).links[0].url == DRIVE_LINK
    assert told(record, "uploaded") == [(A, {"date": "20261110", "title": "title v1", "link": DRIVE_LINK,
                                             "stream_id": "v1"})]
    assert [what for what, _, _ in record.told] == ["processed", "uploaded"]


def test_an_upload_without_a_drive_link_tells_nothing(tmp_path):
    ad, _, sent, record = recorded(tmp_path, FakeDrive(DriveCopy("录播/A/v.zip", None, "分享被拒绝：文件审核未通过")))
    downloaded(ad)
    step(ad, "transcribe")
    step(ad, "drive")

    assert drive_uploaded(sent).links == ()
    assert told(record, "uploaded") == []


def test_an_upload_that_gets_through_only_on_a_retry_is_told_once_when_it_gets_through(tmp_path):
    drive = FakeDrive(DriveCopy("录播/A/v.zip", DRIVE_LINK), error=RuntimeError("网络超时"))
    ad, clock, sent, record = recorded(tmp_path, drive)
    downloaded(ad)
    step(ad, "transcribe")
    step(ad, "drive")
    assert kinds(sent)[-1] is EventKind.DRIVE_FAILED
    assert told(record, "uploaded") == []

    drive.error = None
    clock.t += 900
    step(ad, "drive")
    assert kinds(sent)[-1] is EventKind.DRIVE_UPLOADED
    assert [values["link"] for _, values in told(record, "uploaded")] == [DRIVE_LINK]
    assert ad.load() == []


def test_a_streamer_without_drive_copies_is_told_only_as_processed(tmp_path):
    ad, _, _, record = recorded(tmp_path, FakeDrive())
    downloaded(ad, B)  # only A is ticked for the drive
    step(ad, "transcribe")

    assert ad.load() == []
    assert [(what, streamer) for what, streamer, _ in record.told] == [("processed", B)]


def test_a_failing_record_holds_up_neither_the_drive_message_nor_the_queue(tmp_path):
    ad, _, sent, _ = recorded(tmp_path, FakeDrive())
    ad.source_record = Recorder(RuntimeError("the sheet is broken"))
    downloaded(ad)
    step(ad, "transcribe")
    step(ad, "drive")

    assert kinds(sent)[-1] is EventKind.DRIVE_UPLOADED
    assert ad.load() == []
