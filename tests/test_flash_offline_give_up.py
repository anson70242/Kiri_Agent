"""A live that has waited 72 hours for the flash account stops waiting: its "全部处理完成" goes
out with the short download link and the source record sheet is told it, as for a flash transfer
given up on, but without the alert of a failure. The 72 hours count from the live's own first
wait, also across a restart. Through the pipeline with a fake clock and a fake flash service
whose account stays offline."""

from __future__ import annotations

import pytest

from backend.archive import Archive, ArtifactMissing
from backend.notify import EventKind

from .flash_stage_fakes import (HOUR, AccountFlash, alerts_of, flash_pipeline, pass_time, processed_live, ready,
                                staging_exists, stages, uploading)
from .helpers import A, B
from .test_drive_stage import downloaded, kinds, rewrite_queue, step
from .test_flash_ready_sheet import short_links
from .test_source_record_pipeline import Recorder

SHORT_LINK = "https://kiri.example/d/Code000001"


def failure_alerts(alerts: list[str]) -> list[str]:
    """The operator alerts that are not the reminder of the waiting."""
    return [a for a in alerts if not a.startswith("闪传账号掉线或闪传服务连不上")]


@pytest.mark.parametrize("started", [False, True], ids=["offline at the start", "offline while uploading"])
def test_after_72_hours_ready_goes_out_with_the_short_download_link_without_an_alert_of_a_failure(tmp_path, started):
    flash = AccountFlash(uploading(), state="online" if started else "offline")
    ad, clock, sent, folder = processed_live(tmp_path, flash, full=True)
    ad.source_record = record = Recorder()
    made = short_links(ad)
    alerts = alerts_of(ad)
    if started:
        step(ad, "flash")  # started
        step(ad, "flash")  # uploading
        flash.state = "offline"
        pass_time(ad, clock, 10)  # the first wait
    else:
        step(ad, "flash")  # the first wait
    pass_time(ad, clock, 72 * HOUR - 1)
    assert stages(ad) == ["flash"] and kinds(sent) == [EventKind.DOWNLOADED]

    pass_time(ad, clock, 60)

    assert stages(ad) == []
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    assert [(x.url, x.label) for x in ready(sent).links][0] == (SHORT_LINK, "全部下载（影片、弹幕、中日字幕 SRT）")
    assert made == ["A/20261110/歌枠"] and "闪传" not in ready(sent).detail
    ((_, _, values),) = record.told
    assert values["link"] == SHORT_LINK and "flash" not in values
    assert failure_alerts(alerts) == []
    assert not staging_exists(ad)
    with pytest.raises(ArtifactMissing):
        Archive.from_folder(folder).read_flash_link()


def test_each_live_counts_its_72_hours_from_its_own_first_wait(tmp_path):
    flash = AccountFlash()
    ad, clock, sent = flash_pipeline(tmp_path, flash, ticked=(A, B))
    downloaded(ad, A)
    step(ad, "transcribe")
    step(ad, "flash")  # A first waits
    pass_time(ad, clock, 10 * HOUR)
    downloaded(ad, B)
    step(ad, "transcribe")
    step(ad, "flash")  # B first waits, ten hours later

    pass_time(ad, clock, 62 * HOUR)
    assert [e.status.streamer for e in sent if e.kind is EventKind.READY] == [A]
    pass_time(ad, clock, 10 * HOUR)
    assert [e.status.streamer for e in sent if e.kind is EventKind.READY] == [A, B]


def test_the_72_hours_go_on_from_the_first_wait_after_a_restart(tmp_path):
    ad, clock, _, _ = processed_live(tmp_path, AccountFlash())
    step(ad, "flash")  # the first wait

    restarted, clock2, sent2 = flash_pipeline(tmp_path, AccountFlash())
    alerts_of(restarted)
    clock2.t = clock.t + 71 * HOUR
    pass_time(restarted, clock2, 0)
    assert stages(restarted) == ["flash"]
    pass_time(restarted, clock2, HOUR)

    assert stages(restarted) == [] and kinds(sent2) == [EventKind.READY]


def test_a_queue_file_written_before_this_loads_and_its_flash_job_starts_to_wait(tmp_path):
    ad, clock, _, _ = processed_live(tmp_path, AccountFlash(state="online"))
    rewrite_queue(tmp_path, lambda job: [job.pop(name) for name in ("flash_offline_since", "flash_reminded_at")])

    restarted, clock2, _ = flash_pipeline(tmp_path, AccountFlash())
    alerts = alerts_of(restarted)
    clock2.t = clock.t
    step(restarted, "flash")

    assert stages(restarted) == ["flash"] and len(alerts) == 1
