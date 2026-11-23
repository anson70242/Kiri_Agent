"""A flash service whose status says the flash account is online while a start, or a started
flash transfer's progress, cannot reach it: the live waits as for an offline account. A look
that merely finds the status online does not end the waiting: only a flash transfer started or a
started one's progress answered does. So the owner is reminded at most once a day, the files are
prepared again at most once per retry pause, a started flash transfer is never started again, and
after 72 hours the live's "全部处理完成" goes out with the short download link. Through the
pipeline with a fake clock and a fake flash service whose status and service are scripted."""

from __future__ import annotations

from backend.downloader import auto as auto_mod
from backend.notify import EventKind

from .flash_stage_fakes import (HOUR, PAUSE, AccountFlash, alerts_of, finished, flash_link, flash_pipeline,
                                pass_time, processed_live, ready, stages, uploading)
from .helpers import A, B
from .test_drive_stage import downloaded, kinds, step
from .test_flash_ready_sheet import short_links

SHORT_LINK = "https://kiri.example/d/Code000001"


def unserved(*progress, state: str = "online") -> AccountFlash:
    """A fake flash service whose status says `state` and that serves no start and no progress."""
    flash = AccountFlash(*progress, state=state)
    flash.serving = False
    return flash


def failure_alerts(alerts: list[str]) -> list[str]:
    """The operator alerts that are not the reminder of the waiting."""
    return [a for a in alerts if not a.startswith("闪传账号掉线或闪传服务连不上")]


def preparing_times(monkeypatch, clock) -> list[float]:
    """The times the pipeline prepares a flash transfer's files (and would cut a big video)."""
    times: list[float] = []
    prepare = auto_mod.prepare

    def recording(*args, **kwargs):
        times.append(clock.t)
        return prepare(*args, **kwargs)

    monkeypatch.setattr(auto_mod, "prepare", recording)
    return times


def run_for(ad, clock, seconds: float) -> None:
    """The flash stage's worker for `seconds`, each job tried whenever it comes due."""
    end = clock.t + seconds
    for _ in range(5000):
        if ad.next_due("flash") is not None:
            step(ad, "flash")
            continue
        if not any(j.stage == "flash" for j in ad.load()):
            return
        wait = ad.sleep_time("flash")
        if clock.t + wait > end:
            clock.t = end
            return
        clock.t += wait
    raise AssertionError("the flash stage keeps finding jobs due")


def test_a_start_that_cannot_reach_the_flash_service_reminds_once_a_day_and_prepares_once_a_retry_pause(
        tmp_path, monkeypatch):
    flash = unserved()
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    alerts = alerts_of(ad)
    prepared = preparing_times(monkeypatch, clock)

    run_for(ad, clock, 2 * HOUR)

    assert len(alerts) == 1
    assert len(prepared) >= 2 and all(b - a >= PAUSE for a, b in zip(prepared, prepared[1:]))
    assert flash.starts == [] and flash.unserved == len(prepared)
    (job,) = ad.load()
    assert (job.attempts, job.errors) == (0, 0) and ad.flash_waiting() == 1
    assert kinds(sent) == [EventKind.DOWNLOADED]

    run_for(ad, clock, 22 * HOUR - 60)
    assert len(alerts) == 1
    run_for(ad, clock, PAUSE)
    assert len(alerts) == 2


def test_a_start_that_cannot_reach_the_flash_service_is_given_up_on_72_hours_after_the_first_wait(tmp_path):
    flash = unserved()
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    made = short_links(ad)
    alerts = alerts_of(ad)
    step(ad, "flash")  # the first wait
    run_for(ad, clock, HOUR)
    pass_time(ad, clock, 70 * HOUR)
    pass_time(ad, clock, HOUR - 60)
    assert stages(ad) == ["flash"] and kinds(sent) == [EventKind.DOWNLOADED]

    pass_time(ad, clock, 60)

    assert stages(ad) == []
    assert [(x.url, x.label) for x in ready(sent).links][0] == (SHORT_LINK, "全部下载（影片、弹幕、中日字幕 SRT）")
    assert made == ["A/20261110/歌枠"] and failure_alerts(alerts) == []


def test_a_started_flash_transfer_whose_progress_cannot_be_reached_is_never_started_again_nor_reset_by_the_next_live(
        tmp_path):
    flash = AccountFlash(uploading(), state="online")
    ad, clock, sent = flash_pipeline(tmp_path, flash, ticked=(A, B))
    short_links(ad)
    alerts = alerts_of(ad)
    downloaded(ad, A)
    step(ad, "transcribe")
    step(ad, "flash")  # A's started
    flash.serving = False
    pass_time(ad, clock, 10)  # A's first wait: its progress cannot be reached
    pass_time(ad, clock, HOUR)
    downloaded(ad, B)  # B's 72 hours end an hour after A's
    step(ad, "transcribe")

    run_for(ad, clock, 2 * HOUR)  # B is looked at beside it and finds the status online
    pass_time(ad, clock, 2 * HOUR)
    pass_time(ad, clock, 2 * HOUR)
    assert len(alerts) == 1
    pass_time(ad, clock, 65 * HOUR - 60)
    assert [e.status.streamer for e in sent if e.kind is EventKind.READY] == []

    pass_time(ad, clock, 60)

    assert [e.status.streamer for e in sent if e.kind is EventKind.READY] == [A]
    assert len(flash.starts) == 1 and set(flash.looks) <= {"fs-1"}
    assert failure_alerts(alerts) == []
    assert [(j.status.streamer, j.attempts, j.errors) for j in ad.load()] == [(B, 0, 0)]


def test_once_the_flash_service_serves_again_the_lives_go_on_in_their_order(tmp_path):
    flash = unserved()
    ad, clock, sent = flash_pipeline(tmp_path, flash, ticked=(A, B))
    alerts_of(ad)
    downloaded(ad, A)
    step(ad, "transcribe")
    step(ad, "flash")
    downloaded(ad, B)
    step(ad, "transcribe")
    run_for(ad, clock, HOUR)

    flash.serving = True
    run_for(ad, clock, PAUSE + 60)

    assert [(e.status.streamer, e.links[0].url) for e in sent if e.kind is EventKind.READY] == [
        (A, flash_link(1)), (B, flash_link(2))]
    assert stages(ad) == []


def test_a_finished_upload_after_a_progress_that_could_not_be_reached_goes_out_with_its_flash_link(tmp_path):
    flash = AccountFlash(uploading(), state="online")
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    alerts_of(ad)
    step(ad, "flash")  # started
    flash.serving = False
    run_for(ad, clock, HOUR)
    flash.serving = True
    flash.progress_answers = [finished()]
    run_for(ad, clock, 60)

    assert len(flash.starts) == 1
    assert ready(sent).links[0].url == flash_link(1)
