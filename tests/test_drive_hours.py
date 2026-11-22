"""The drive upload hours: with `drive.upload_hours` [3, 9], the upload of a drive copy starts
only between 03:00 and 09:00 Hong Kong time; outside them the drive job waits for 03:00,
while "全部处理完成" goes out as always. Through the automatic download with a fake clock,
fake stages and a fake drive."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.downloader.auto import load_auto
from backend.drive import DriveFailed
from backend.notify import EventKind

from .test_drive_failures import ScriptedDrive
from .test_drive_stage import FakeDrive, downloaded, kinds, make, step

HONG_KONG = timezone(timedelta(hours=8))


def hk(day: int, hour: int, minute: int = 0) -> float:
    """That time of that day of November 2026 in Hong Kong, as the clock's seconds."""
    return datetime(2026, 11, day, hour, minute, tzinfo=HONG_KONG).timestamp()


def processed_at(tmp_path, drive, when: float):
    """A's live, downloaded and transcribed at `when`, with upload hours 03:00 to 09:00."""
    ad, clock, sent = make(tmp_path, drive, drive_hours=(3, 9))
    clock.t = when
    downloaded(ad)
    step(ad, "transcribe")
    return ad, clock, sent


def test_a_live_processed_in_the_evening_is_ready_at_once_and_its_upload_starts_at_three(tmp_path):
    drive = FakeDrive()
    ad, clock, sent = processed_at(tmp_path, drive, hk(22, 22, 30))
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]  # not held by the hours

    step(ad, "drive")
    (job,) = ad.load()
    assert drive.calls == [] and (job.attempts, job.errors) == (0, 0) and len(sent) == 2
    clock.t = hk(23, 2, 59)
    assert ad.next_due("drive") is None  # not tried again before the hours begin

    clock.t = hk(23, 3)
    step(ad, "drive")
    assert len(drive.calls) == 1 and ad.load() == []
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY, EventKind.DRIVE_UPLOADED]


@pytest.mark.parametrize("when", [hk(23, 3), hk(23, 5, 30), hk(23, 8, 59)], ids=["03:00", "05:30", "08:59"])
def test_inside_the_hours_the_upload_starts_at_once(tmp_path, when):
    drive = FakeDrive()
    ad, _, sent = processed_at(tmp_path, drive, when)
    step(ad, "drive")

    assert len(drive.calls) == 1 and ad.load() == []
    assert kinds(sent)[-1] is EventKind.DRIVE_UPLOADED


@pytest.mark.parametrize("when, begins", [(hk(23, 9), hk(24, 3)), (hk(23, 14), hk(24, 3)),
                                           (hk(23, 23, 59), hk(24, 3)), (hk(23, 0, 10), hk(23, 3))],
                         ids=["09:00", "14:00", "23:59", "00:10"])
def test_outside_the_hours_the_upload_waits_for_the_next_three_o_clock(tmp_path, when, begins):
    drive = FakeDrive()
    ad, clock, _ = processed_at(tmp_path, drive, when)
    step(ad, "drive")

    assert drive.calls == []
    clock.t = begins - 60
    assert ad.next_due("drive") is None
    clock.t = begins
    step(ad, "drive")
    assert len(drive.calls) == 1 and ad.load() == []


def test_a_retry_that_falls_after_the_hours_waits_for_the_next_day_without_counting_or_a_message(tmp_path):
    drive = ScriptedDrive(DriveFailed("网络超时"), FakeDrive().copy)
    ad, clock, sent = processed_at(tmp_path, drive, hk(23, 8, 50))
    step(ad, "drive")  # fails at 08:50: retried in 15 minutes
    assert kinds(sent)[-1] is EventKind.DRIVE_FAILED

    clock.t = hk(23, 9, 5)
    step(ad, "drive")  # the retry's turn, after the hours
    (job,) = ad.load()
    assert len(drive.calls) == 1 and (job.attempts, job.errors) == (1, 1) and kinds(sent)[-1] is EventKind.DRIVE_FAILED
    clock.t = hk(24, 2, 59)
    assert ad.next_due("drive") is None

    clock.t = hk(24, 3)
    step(ad, "drive")
    assert len(drive.calls) == 2 and ad.load() == []
    assert kinds(sent)[-1] is EventKind.DRIVE_UPLOADED


def test_the_hours_are_hong_kong_time_whatever_the_servers_clock_is_set_to(tmp_path):
    drive = FakeDrive()
    three_in_hong_kong = datetime(2026, 11, 22, 19, 0, tzinfo=timezone.utc).timestamp()  # 03:00 on the 23rd there
    ad, _, _ = processed_at(tmp_path, drive, three_in_hong_kong)
    step(ad, "drive")

    assert len(drive.calls) == 1


# ---- config.json ----

def config(tmp_path, drive: dict) -> object:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True}]},
        "drive": drive}), encoding="utf-8")
    return cfg


def test_the_hours_come_from_the_config_and_without_them_an_upload_starts_at_any_time(tmp_path):
    assert load_auto(config(tmp_path, {"upload_hours": [3, 9]})).drive_hours == (3, 9)
    assert load_auto(config(tmp_path, {})).drive_hours is None


@pytest.mark.parametrize("hours", [[9, 3], [3], [3, 25], ["3", "9"], [3, 3], "3-9", [True, 9]],
                         ids=lambda h: json.dumps(h))
def test_hours_that_are_not_two_rising_hours_of_a_day_are_refused(tmp_path, hours):
    with pytest.raises(ValueError, match="upload_hours"):
        load_auto(config(tmp_path, {"upload_hours": hours}))
