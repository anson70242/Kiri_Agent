"""The drive stage when an upload does not succeed: "全部处理完成" at once with the site's
links and 网盘上传失败, retries in the background, 网盘上传完成 when one gets through, and a
lapsed authorization told apart from an error. Through the automatic download with a fake
clock, fake stages and a fake drive."""

from __future__ import annotations

import pytest

from backend.downloader import auto as auto_mod
from backend.downloader.base import DownloadResult
from backend.notify import StreamEvent

from backend.drive import DriveAuthorizationLapsed, DriveCopy, DriveFailed
from backend.notify import EventKind

from .helpers import A, B, bare_archive, later, live
from .test_drive_stage import ReplaySource, downloaded, kinds, links, make, processed, ready_as_today, step

HOUR = 3600
LINK = "https://drive.example/s/abc?pwd=x1y2"
COPY = DriveCopy("录播/A/[20261110][A] 歌枠.zip", LINK)


class ScriptedDrive:
    """Answers each upload with the next of `answers`: a DriveCopy, or an exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, archive, folder, progress):
        self.calls.append((archive.folder, folder))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def after(sent, n):
    """The kinds sent after the first `n` events."""
    return kinds(sent)[n:]


# ---- the first attempt fails ----

@pytest.mark.parametrize("error", [DriveFailed("32003 空间不足"), RuntimeError("32003 空间不足")],
                         ids=["failed", "a bug"])
def test_a_failed_first_attempt_sends_ready_at_once_and_drive_upload_failed_and_keeps_the_job(tmp_path, error):
    drive = ScriptedDrive(error)
    ad, clock, sent, _ = processed(tmp_path, drive)
    step(ad, "drive")

    assert after(sent, 1) == [EventKind.READY, EventKind.DRIVE_FAILED]
    ready_as_today(sent)
    failed = sent[-1]
    assert "32003 空间不足" in failed.detail and "自动重试" in failed.detail
    (job,) = ad.load()
    assert (job.stage, job.attempts, job.errors) == ("drive", 1, 1)
    assert ad.next_due("drive") is None
    clock.t += 900
    assert ad.next_due("drive") is not None


def retry(ad, clock):
    clock.t += 900
    step(ad, "drive")


def test_later_errors_are_silent_and_the_last_one_gives_up_with_a_second_drive_upload_failed(tmp_path):
    drive = ScriptedDrive(DriveFailed("32003 空间不足"), DriveFailed("网络超时"), DriveFailed("32004 单文件超限"))
    ad, clock, sent, _ = processed(tmp_path, drive)
    step(ad, "drive")
    retry(ad, clock)
    assert after(sent, 3) == []  # the second error says nothing
    retry(ad, clock)

    assert ad.load() == []  # given up: the archive goes by the usual rule at the next prune
    assert after(sent, 1) == [EventKind.READY, EventKind.DRIVE_FAILED, EventKind.DRIVE_FAILED]
    gave_up = sent[-1]
    assert "放弃" in gave_up.detail and "32004 单文件超限" in gave_up.detail and "3 次" in gave_up.detail


def test_a_retry_that_gets_through_sends_drive_copy_uploaded_with_the_link_and_no_second_ready(tmp_path):
    drive = ScriptedDrive(DriveFailed("网络超时"), COPY)
    ad, clock, sent, folder = processed(tmp_path, drive)
    step(ad, "drive")
    retry(ad, clock)

    assert ad.load() == []
    assert after(sent, 1) == [EventKind.READY, EventKind.DRIVE_FAILED, EventKind.DRIVE_UPLOADED]
    uploaded = sent[-1]
    assert links(uploaded) == [(LINK, "网盘链接")]
    assert "录播/A/[20261110][A] 歌枠.zip" in uploaded.detail
    # A retry is the same call again: the drive checks what is already there before uploading.
    assert drive.calls == [(folder, "录播/A"), (folder, "录播/A")]


def test_ready_is_not_sent_again_after_a_restart(tmp_path):
    ad, clock, sent, _ = processed(tmp_path, ScriptedDrive(DriveFailed("网络超时")))
    step(ad, "drive")
    restarted, clock2, sent2 = make(tmp_path, ScriptedDrive(COPY))
    clock2.t = clock.t + 900
    step(restarted, "drive")

    assert kinds(sent2) == [EventKind.DRIVE_UPLOADED]
    assert links(sent2[0]) == [(LINK, "网盘链接")]


# ---- the authorization lapsed ----

LAPSED = DriveAuthorizationLapsed("-1408 授权已失效")


def test_a_lapse_on_the_first_try_sends_ready_drive_upload_failed_and_authorization_lapsed(tmp_path):
    ad, clock, sent, _ = processed(tmp_path, ScriptedDrive(LAPSED))
    step(ad, "drive")

    assert after(sent, 1) == [EventKind.READY, EventKind.DRIVE_FAILED, EventKind.DRIVE_AUTHORIZATION_LAPSED]
    ready_as_today(sent)
    assert "授权" in sent[2].detail and "继续上传" in sent[2].detail
    assert "login" in sent[3].detail  # the command to run
    (job,) = ad.load()
    assert (job.attempts, job.errors) == (0, 0)  # a lapsed try is neither
    clock.t += 900
    assert ad.next_due("drive") is not None


def two_lives(tmp_path, drive):
    """A's and B's lives, both ticked for the drive, processed: two drive jobs wait."""
    ad, clock, sent = make(tmp_path, drive, folders={A.id: "录播/A", B.id: "录播/B"})
    for s in (A, B):
        downloaded(ad, s)
        step(ad, "transcribe")
    assert [j.stage for j in ad.load()] == ["drive", "drive"]
    return ad, clock, sent


def by_kind(sent, kind):
    return [e.status.streamer.id for e in sent if e.kind is kind]


def test_one_authorization_lapsed_for_two_waiting_jobs_and_a_drive_upload_failed_for_each(tmp_path):
    ad, clock, sent = two_lives(tmp_path, ScriptedDrive(LAPSED, LAPSED, LAPSED, LAPSED))
    for _ in range(2):
        step(ad, "drive")
        step(ad, "drive")
        clock.t += 900

    assert by_kind(sent, EventKind.READY) == [A.id, B.id]
    assert by_kind(sent, EventKind.DRIVE_FAILED) == [A.id, B.id]
    assert len(by_kind(sent, EventKind.DRIVE_AUTHORIZATION_LAPSED)) == 1
    assert len(ad.load()) == 2


def test_a_lapsed_job_is_tried_again_and_given_up_72_hours_after_its_first_lapsed_try(tmp_path):
    drive = ScriptedDrive(*[LAPSED] * 400)
    ad, clock, sent, _ = processed(tmp_path, drive)
    start = clock.t
    step(ad, "drive")
    while clock.t - start < 72 * HOUR - 900:
        retry(ad, clock)
        assert len(ad.load()) == 1  # far more tries than the errors allowed: lapses do not count
    assert after(sent, 4) == []
    retry(ad, clock)

    assert ad.load() == []
    assert after(sent, 4) == [EventKind.DRIVE_FAILED]
    assert "72 小时" in sent[-1].detail and "放弃" in sent[-1].detail


def test_an_upload_that_gets_through_ends_the_lapse_and_the_next_lapse_is_told_again(tmp_path):
    ad, clock, sent = two_lives(tmp_path, ScriptedDrive(LAPSED, COPY, LAPSED))
    step(ad, "drive")  # A: lapsed, told
    clock.t += 900
    step(ad, "drive")  # B: the owner authorized again, B goes through
    step(ad, "drive")  # A: lapsed again, a new lapse

    assert len(by_kind(sent, EventKind.DRIVE_AUTHORIZATION_LAPSED)) == 2


def test_a_lapsed_job_goes_through_once_authorized_again_with_drive_copy_uploaded(tmp_path):
    ad, clock, sent, _ = processed(tmp_path, ScriptedDrive(LAPSED, LAPSED, COPY))
    step(ad, "drive")
    retry(ad, clock)
    retry(ad, clock)

    assert ad.load() == []
    assert after(sent, 4) == [EventKind.DRIVE_UPLOADED]
    assert links(sent[-1]) == [(LINK, "网盘链接")]


# ---- the archive stays until its upload is given up on ----

def test_an_archive_whose_upload_is_retried_stays_and_goes_by_the_usual_rule_once_given_up(tmp_path, monkeypatch):
    dates = iter(f"202611{d:02d}" for d in range(1, 31))

    def fake_download(url, settings, streamers, info=None, progress=None):
        folder = bare_archive(settings.output_dir, "A", next(dates), info.id, stem=info.id, video=b"x")
        return DownloadResult(info, folder, video=folder / f"{info.id}.mp4")

    monkeypatch.setattr(auto_mod, "downloader_for", lambda *a: ReplaySource())
    monkeypatch.setattr(auto_mod, "download", fake_download)
    ad, clock, _ = make(tmp_path, ScriptedDrive(*[DriveFailed("网络超时")] * 3, COPY))
    ad.keep = {A.id: 1}

    def end_and_download(sid):
        ad.on_ended(StreamEvent(EventKind.OFFLINE, live(A, sid=sid), later(60)))
        job = ad.next_due("download")
        outcome = ad.attempt(job)
        ad.finish(job, outcome)
        return outcome.result.output_dir

    first = end_and_download("v1")
    step(ad, "transcribe")
    step(ad, "drive")  # fails: retried later
    second = end_and_download("v2")
    assert first.exists()  # its drive copy is not on the drive yet

    retry(ad, clock)
    retry(ad, clock)  # the third error: given up on
    assert [j.status.stream_id for j in ad.load()] == ["v2"]
    end_and_download("v3")
    assert not first.exists() and second.exists()  # the second still has a job
