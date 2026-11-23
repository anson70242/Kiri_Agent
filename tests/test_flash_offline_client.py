"""A flash job's look at the flash account asks the flash service whether the account is online
and nothing more: while it waits, unattended for up to 72 hours, it never calls the login
interface (only the 通知 page does). Through the pipeline with the real flash service client
against the fake flash service, its account offline."""

from __future__ import annotations

from backend.flash.client import FlashClient

from .flash_fakes import FakeFlashService
from .flash_stage_fakes import alerts_of, pass_time, processed_live, stages
from .test_drive_stage import step


def test_a_waiting_flash_jobs_look_makes_no_call_to_the_login_interface(tmp_path):
    with FakeFlashService() as service:
        service.online = False
        ad, clock, _, _ = processed_live(tmp_path, FlashClient(service.settings(), tmp_path / "dl"), full=True)
        alerts_of(ad)
        step(ad, "flash")
        pass_time(ad, clock, 60)
        pass_time(ad, clock, 60)

        assert stages(ad) == ["flash"] and ad.load()[0].errors == 0
        assert [frame.get("action") for frame in service.actions] == ["get_status"] * 3
        assert service.login_calls == []
