"""While lives wait for the flash account the owner is reminded on Discord's operator alert: at
once when a live first starts to wait, then every 24 hours while any live still waits, never
more often, one reminder for all waiting lives, also across a restart. No QQ group is told.
Through the pipeline with a fake clock and a fake flash service whose account is offline."""

from __future__ import annotations

from backend.notify import EventKind

from .flash_stage_fakes import HOUR, AccountFlash, alerts_of, flash_pipeline, pass_time, processed_live, uploading
from .helpers import A, B
from .test_drive_stage import SITE, downloaded, kinds, step


def test_the_owner_is_reminded_at_once_with_the_address_of_the_notify_page_and_no_group_is_told(tmp_path):
    ad, clock, sent, _ = processed_live(tmp_path, AccountFlash())
    alerts = alerts_of(ad)
    step(ad, "flash")

    (reminder,) = alerts
    assert reminder.splitlines()[0] == "闪传账号掉线或闪传服务连不上"
    assert "1 场录播在等闪传" in reminder
    assert f"{SITE}/notify" in reminder
    assert kinds(sent) == [EventKind.DOWNLOADED]  # nothing for the groups


def test_without_site_url_the_reminder_has_no_address(tmp_path):
    ad, clock, _, _ = processed_live(tmp_path, AccountFlash())
    ad.site_url = ""
    alerts = alerts_of(ad)
    step(ad, "flash")

    (reminder,) = alerts
    assert "闪传账号掉线或闪传服务连不上" in reminder and "http" not in reminder


def test_the_reminder_comes_again_every_24_hours_and_never_more_often(tmp_path):
    ad, clock, _, _ = processed_live(tmp_path, AccountFlash())
    alerts = alerts_of(ad)
    step(ad, "flash")

    pass_time(ad, clock, 24 * HOUR - 60)
    assert len(alerts) == 1
    pass_time(ad, clock, 60)
    assert len(alerts) == 2
    pass_time(ad, clock, 23 * HOUR)
    assert len(alerts) == 2
    pass_time(ad, clock, HOUR)
    assert len(alerts) == 3


def test_one_reminder_covers_every_waiting_live_and_a_new_one_does_not_bring_another_within_the_day(tmp_path):
    flash = AccountFlash()
    ad, clock, _ = flash_pipeline(tmp_path, flash, ticked=(A, B))
    alerts = alerts_of(ad)
    downloaded(ad, A)
    step(ad, "transcribe")
    step(ad, "flash")
    pass_time(ad, clock, 2 * HOUR)
    downloaded(ad, B)
    step(ad, "transcribe")
    step(ad, "flash")  # B starts to wait, two hours after the reminder
    assert len(alerts) == 1 and "1 场录播在等闪传" in alerts[0]

    pass_time(ad, clock, 22 * HOUR)
    assert len(alerts) == 2 and "2 场录播在等闪传" in alerts[1]


def test_no_second_reminder_within_the_day_after_a_restart(tmp_path):
    ad, clock, _, _ = processed_live(tmp_path, AccountFlash())
    first = alerts_of(ad)
    step(ad, "flash")

    restarted, clock2, _ = flash_pipeline(tmp_path, AccountFlash())
    alerts = alerts_of(restarted)
    clock2.t = clock.t + HOUR
    pass_time(restarted, clock2, 0)
    pass_time(restarted, clock2, 22 * HOUR)
    assert len(first) == 1 and alerts == []
    pass_time(restarted, clock2, HOUR)
    assert len(alerts) == 1


def test_a_new_outage_after_the_scan_is_reminded_at_once(tmp_path):
    flash = AccountFlash()
    ad, clock, _, _ = processed_live(tmp_path, flash)
    alerts = alerts_of(ad)
    step(ad, "flash")
    flash.state = "online"
    flash.progress_answers = [uploading()]
    pass_time(ad, clock, 60)  # back: started
    flash.state = "offline"
    pass_time(ad, clock, 10)

    assert len(alerts) == 2
