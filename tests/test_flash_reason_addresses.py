"""An address in the reason of a flash transfer's failed try or waiting (http, https, ws or
wss) reaches neither the log, nor the queue file and the members' queue view (`last_error`),
nor the owner's alert; an unexpected error is logged by its kind and its text without
addresses, with no traceback. Through the pipeline with a fake clock and a fake flash service
whose errors name made-up addresses."""

from __future__ import annotations

import logging

import pytest

from backend.flash.client import FlashFailed, FlashUnavailable

from .flash_stage_fakes import FakeFlash, alerts_of, pass_time, processed_live, stages
from .test_drive_stage import step

ADDRESSES = ["ws://flash.example:8001/x", "wss://flash.example:8001/x", "http://flash.example:8002/y",
             "https://flash.example/s/abc"]


def queue_text(tmp_path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("download_queue.json"))


@pytest.mark.parametrize("address", ADDRESSES)
def test_an_address_in_a_failed_tries_reason_is_left_out_everywhere(tmp_path, caplog, address):
    caplog.set_level(logging.DEBUG)
    flash = FakeFlash(start=[FlashFailed(f"拒绝了 {address}") for _ in range(3)])
    ad, clock, _, _ = processed_live(tmp_path, flash, full=True)
    alerts = alerts_of(ad)
    step(ad, "flash")

    (job,) = ad.load()
    queued = queue_text(tmp_path) + str(ad.snapshot())
    for _ in range(2):
        pass_time(ad, clock, ad.retry_interval)

    assert "拒绝了" in job.last_error and "flash.example" not in queued
    assert stages(ad) == [] and len(alerts) == 1 and "flash.example" not in alerts[0]
    assert "flash.example" not in caplog.text


@pytest.mark.parametrize("address", ADDRESSES)
def test_an_address_in_a_waitings_reason_is_left_out_of_the_log_and_the_queue(tmp_path, caplog, address):
    caplog.set_level(logging.DEBUG)
    flash = FakeFlash(start=[FlashUnavailable(f"连不上 {address}")])
    ad, clock, _, _ = processed_live(tmp_path, flash, full=True)
    alerts_of(ad)
    step(ad, "flash")

    (job,) = ad.load()
    assert job.flash_offline_since is not None and "连不上" in job.last_error
    assert "flash.example" not in queue_text(tmp_path) + str(ad.snapshot()) + caplog.text


@pytest.mark.parametrize("address", ADDRESSES)
def test_an_unexpected_error_is_logged_by_its_kind_and_its_text_without_the_address(tmp_path, caplog, address):
    caplog.set_level(logging.DEBUG)
    flash = FakeFlash(start=[RuntimeError(f"boom at {address}")])
    ad, clock, _, _ = processed_live(tmp_path, flash, full=True)
    alerts_of(ad)
    step(ad, "flash")

    (job,) = ad.load()
    assert job.errors == 1 and "RuntimeError" in job.last_error
    assert "RuntimeError" in caplog.text and "boom at" in caplog.text
    assert "flash.example" not in caplog.text + queue_text(tmp_path)
    assert "Traceback" not in caplog.text
