"""The 「闪传账号」 block's endpoint says how many lives wait for the flash account: the
pipeline's flash jobs that found it offline, read from the download queue the dashboard's
pipeline keeps. With the dashboard's test client, a fake flash service client and a queue file
the pipeline wrote."""

from __future__ import annotations

from backend.flash.client import AccountState

from .flash_stage_fakes import AccountFlash, flash_pipeline, processed_live
from .helpers import A, B
from .test_drive_stage import downloaded, step
from .test_flash_account_api import FakeFlashClient, account, app, use_flash


def two_waiting_lives(tmp_path) -> None:
    """A's and B's lives wait for the flash account in the queue file at tmp_path/queue.json
    (the one the dashboard's written config names)."""
    ad, _, _ = flash_pipeline(tmp_path, AccountFlash(), ticked=(A, B))
    for s in (A, B):
        downloaded(ad, s)
        step(ad, "transcribe")
        step(ad, "flash")


def test_the_block_says_how_many_lives_wait_for_the_account(tmp_path, monkeypatch):
    two_waiting_lives(tmp_path)
    use_flash(monkeypatch, FakeFlashClient(AccountState("offline")))

    body = account(app(tmp_path))

    assert body["state"] == "offline" and body["waiting"] == 2


def test_no_live_waits_while_the_flash_transfers_go_on(tmp_path, monkeypatch):
    processed_live(tmp_path, AccountFlash(state="online"))
    use_flash(monkeypatch, FakeFlashClient(AccountState("online")))

    assert account(app(tmp_path))["waiting"] == 0


def test_without_a_flash_service_nothing_waits(tmp_path, monkeypatch):
    use_flash(monkeypatch, None)

    body = account(app(tmp_path))

    assert body["state"] == "unconfigured" and body["waiting"] == 0


def test_a_new_qr_code_answers_the_number_too(tmp_path, monkeypatch):
    two_waiting_lives(tmp_path)
    use_flash(monkeypatch, FakeFlashClient(AccountState("offline", qr_code="https://qr.example/login?k=made-up-1")))

    r = app(tmp_path).post("/api/notify/flash/qr-code")

    assert r.status_code == 200 and r.json()["waiting"] == 2
