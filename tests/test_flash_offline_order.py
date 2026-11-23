"""Once the flash account is online again, the lives that waited for it are transferred one at
a time, in the order they were processed, and each "全部处理完成" goes out with its own flash
link, without anything else done. Through the pipeline with a fake clock and a fake flash
service whose account is offline for a while."""

from __future__ import annotations

from backend.notify import EventKind

from .flash_stage_fakes import (HOUR, AccountFlash, alerts_of, finished, flash_link, flash_pipeline, pass_time,
                                stages, uploading)
from .helpers import A, B
from .test_drive_stage import downloaded, step


def ready_links(sent):
    """Each "全部处理完成"'s streamer and first link, in the order they went out."""
    return [(e.status.streamer, e.links[0].url) for e in sent if e.kind is EventKind.READY]


def test_the_lives_that_waited_go_in_the_order_they_were_processed_even_when_a_later_one_finds_the_account_first(
        tmp_path):
    flash = AccountFlash()
    ad, clock, sent = flash_pipeline(tmp_path, flash, ticked=(A, B))
    alerts_of(ad)
    downloaded(ad, A)
    step(ad, "transcribe")
    step(ad, "flash")  # A waits, looked at again in a minute
    clock.t += 30
    downloaded(ad, B)
    step(ad, "transcribe")
    step(ad, "flash")  # B waits, its next look half a minute before A's second next one
    pass_time(ad, clock, 30)  # A's look: still offline

    flash.state = "online"
    started_after: list[int] = []
    flash.during_start = lambda files: started_after.append(len(ready_links(sent)))
    pass_time(ad, clock, 30)  # B's look comes first and finds the account online
    for _ in range(3):
        pass_time(ad, clock, 10)

    assert ready_links(sent) == [(A, flash_link(1)), (B, flash_link(2))]
    assert started_after == [0, 1]  # one at a time: B's started once A's was uploaded
    assert stages(ad) == []


def test_a_started_flash_transfer_that_waited_goes_on_first_and_the_next_live_waits_beside_it(tmp_path):
    flash = AccountFlash(uploading(), state="online")
    ad, clock, sent = flash_pipeline(tmp_path, flash, ticked=(A, B))
    alerts = alerts_of(ad)
    downloaded(ad, A)
    step(ad, "transcribe")
    step(ad, "flash")  # A's started
    flash.state = "offline"
    pass_time(ad, clock, 10)  # A waits
    downloaded(ad, B)
    step(ad, "transcribe")
    pass_time(ad, clock, 0)  # B is looked at beside it: it waits too
    assert ad.flash_waiting() == 2 and len(alerts) == 1

    pass_time(ad, clock, 24 * HOUR)
    assert "2 场录播在等闪传" in alerts[-1]
    flash.state = "online"
    flash.progress_answers = [finished()]
    started_after: list[int] = []
    flash.during_start = lambda files: started_after.append(len(ready_links(sent)))
    pass_time(ad, clock, 60)
    pass_time(ad, clock, 10)

    assert ready_links(sent) == [(A, flash_link(1)), (B, flash_link(2))]
    assert started_after == [1] and flash.looks[0] == "fs-1"  # A went on by its id; B started after it
    assert stages(ad) == []
