"""通知 page in a real browser: the 「闪传账号」 block says how many lives wait for the flash
account, and nothing about it while none does. A fake flash service client and a queue file the
pipeline wrote; made-up QR codes. Needs `uv run playwright install chromium`."""

import pytest

from backend.flash.client import AccountState

from .test_flash_account_api import QR_CODE, FakeFlashClient, use_flash
from .test_flash_account_waiting import two_waiting_lives
from .test_monitor_page import browser, notify_server, page  # noqa: F401  (pytest fixtures)

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect


@pytest.fixture
def flash(monkeypatch):
    """The flash account offline with a QR code; list it before notify_server, which builds the app."""
    fake = FakeFlashClient(AccountState("offline", qr_code=QR_CODE))
    use_flash(monkeypatch, fake)
    return fake


def block(page):
    return page.get_by_role("region", name="闪传账号")


def test_the_block_says_how_many_lives_wait_for_the_account(flash, page, notify_server):  # noqa: F811
    url, tmp_path = notify_server
    two_waiting_lives(tmp_path)
    page.goto(url + "/notify")

    expect(block(page)).to_contain_text("2 场录播在等闪传账号")


def test_nothing_is_said_of_waiting_while_no_live_waits(flash, page, notify_server):  # noqa: F811
    url, _ = notify_server
    page.goto(url + "/notify")

    expect(block(page)).to_contain_text("掉线")
    expect(block(page)).not_to_contain_text("在等闪传账号")
