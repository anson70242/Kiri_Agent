"""While the flash account is offline, or the flash service cannot be reached, a flash transfer
waits for it instead of failing: no try is counted, nothing is prepared or cut, the job is
looked at again every minute, and once the account is online the live's "全部处理完成" goes out
with its flash link. Through the pipeline with a fake clock and a fake flash service whose
account is offline for a while."""

from __future__ import annotations

import pytest

from backend.flash.client import Progress
from backend.notify import EventKind

from .flash_stage_fakes import (HOUR, AccountFlash, alerts_of, flash_ready_links, pass_time, processed_live,
                                finished, ready, staging_exists, stages, uploading)
from .test_drive_stage import kinds, links, step


@pytest.mark.parametrize("state", ["offline", "unreachable"])
def test_a_flash_job_waits_for_the_account_without_preparing_anything_or_counting_a_try(tmp_path, state):
    flash = AccountFlash(state=state)
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    alerts_of(ad)
    step(ad, "flash")
    pass_time(ad, clock, 5 * HOUR)  # far beyond the pipeline's three tries fifteen minutes apart

    (job,) = ad.load()
    assert (job.stage, job.attempts, job.errors) == ("flash", 0, 0)
    assert flash.starts == [] and not staging_exists(ad)
    assert kinds(sent) == [EventKind.DOWNLOADED]


def test_a_waiting_flash_job_is_looked_at_again_every_minute(tmp_path):
    flash = AccountFlash()
    ad, clock, _, _ = processed_live(tmp_path, flash)
    step(ad, "flash")
    asked = flash.accounts

    pass_time(ad, clock, 59)
    assert flash.accounts == asked
    pass_time(ad, clock, 1)
    assert flash.accounts == asked + 1


def test_once_the_account_is_online_again_ready_goes_out_with_the_flash_link(tmp_path):
    flash = AccountFlash()
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    alerts_of(ad)
    step(ad, "flash")
    pass_time(ad, clock, 3 * HOUR)

    flash.state = "online"  # the owner scanned
    pass_time(ad, clock, 60)
    pass_time(ad, clock, 10)  # the upload's next look

    assert len(flash.starts) == 1
    assert kinds(sent) == [EventKind.DOWNLOADED, EventKind.READY]
    assert links(ready(sent)) == flash_ready_links()
    assert stages(ad) == []


def test_an_account_that_goes_offline_while_the_upload_is_looked_at_makes_it_wait_not_fail(tmp_path):
    flash = AccountFlash(uploading(50), state="online")
    ad, clock, sent, _ = processed_live(tmp_path, flash, full=True)
    alerts = alerts_of(ad)
    step(ad, "flash")  # started
    step(ad, "flash")  # uploading
    flash.state = "offline"
    pass_time(ad, clock, 5 * HOUR)

    (job,) = ad.load()
    assert (job.attempts, job.errors) == (0, 0)
    assert job.transfer is not None and staging_exists(ad)  # the started flash transfer is kept
    assert kinds(sent) == [EventKind.DOWNLOADED]

    flash.state = "online"
    flash.progress_answers = [finished()]
    pass_time(ad, clock, 60)

    assert len(flash.starts) == 1 and set(flash.looks) == {"fs-1"}  # went on by its id, not started again
    assert links(ready(sent)) == flash_ready_links()
    assert [a for a in alerts if "失败" in a] == []


def test_a_started_flash_transfer_that_fails_after_the_scan_no_longer_counts_as_waiting(tmp_path):
    flash = AccountFlash(uploading(50), state="online")
    ad, clock, _, _ = processed_live(tmp_path, flash, full=True)
    alerts_of(ad)
    step(ad, "flash")  # started
    flash.state = "offline"
    step(ad, "flash")
    assert ad.flash_waiting() == 1

    flash.state = "online"
    flash.progress_answers = [Progress(100, 100, done=4, failed=1, finished=True)]
    pass_time(ad, clock, 60)

    (job,) = ad.load()
    assert job.errors == 1 and ad.flash_waiting() == 0
