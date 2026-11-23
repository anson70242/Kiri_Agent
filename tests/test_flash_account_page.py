"""通知 page in a real browser: the 「闪传账号」 block shows the QR code while the flash account
is offline, a new one on 「刷新二维码」, and turns to 在线 by itself after the scan. A fake flash
service client; made-up QR codes. Needs `uv run playwright install chromium`."""

import pytest

from backend.flash.client import AccountState

from .test_flash_account_api import NEW_QR_CODE, QR_CODE, FakeFlashClient, use_flash
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


def qr_src(page) -> str:
    image = block(page).get_by_role("img", name="闪传账号登录二维码")
    expect(image).to_be_visible()
    assert image.evaluate("i => i.complete && i.naturalWidth > 0")  # an image the browser can draw
    return image.get_attribute("src")


def test_the_qr_code_is_shown_and_the_block_turns_online_after_the_scan(flash, page, notify_server):  # noqa: F811
    url, _ = notify_server
    page.goto(url + "/notify")
    expect(block(page)).to_contain_text("掉线")
    first = qr_src(page)

    flash.state = AccountState("online")  # the owner scans with the phone
    expect(block(page)).to_contain_text("在线", timeout=10000)
    expect(block(page).get_by_role("img")).to_have_count(0)
    expect(block(page).get_by_role("button", name="刷新二维码")).to_have_count(0)
    assert first.startswith("data:image/")

    asked = []  # online: the page stops asking every few seconds
    page.on("request", lambda r: asked.append(r.url) if r.url.endswith("/api/notify/flash") else None)
    page.wait_for_timeout(4500)
    assert asked == []


def test_a_new_qr_code_is_shown_on_refresh(flash, page, notify_server):  # noqa: F811
    url, _ = notify_server
    page.goto(url + "/notify")
    first = qr_src(page)

    block(page).get_by_role("button", name="刷新二维码").click()

    expect(block(page).get_by_role("img")).not_to_have_attribute("src", first)
    assert flash.refreshes == 1 and flash.state.qr_code == NEW_QR_CODE


def test_the_flash_service_s_words_and_what_is_missing_are_shown(flash, page, notify_server):  # noqa: F811
    url, _ = notify_server
    flash.state = AccountState("offline", error="二维码已过期",
                               missing="没有设置登录哈希（.env 里的 FLASH_LOGIN_HASH），看不到二维码")
    page.goto(url + "/notify")
    expect(block(page)).to_contain_text("二维码已过期")
    expect(block(page)).to_contain_text("FLASH_LOGIN_HASH")
    expect(block(page).get_by_role("img")).to_have_count(0)


def no_code_and_no_button(page, text):
    expect(block(page)).to_contain_text(text)
    expect(block(page).get_by_role("img")).to_have_count(0)
    expect(block(page).get_by_role("button")).to_have_count(0)


def test_a_flash_service_that_cannot_be_reached_shows_no_code_and_no_button(flash, page, notify_server):  # noqa: F811
    url, _ = notify_server
    flash.state = AccountState("unreachable")
    page.goto(url + "/notify")
    no_code_and_no_button(page, "连不上闪传服务")


@pytest.fixture
def no_flash(monkeypatch):
    use_flash(monkeypatch, None)


def test_without_a_flash_service_the_block_says_not_set_up(no_flash, page, notify_server):  # noqa: F811
    url, _ = notify_server
    page.goto(url + "/notify")
    no_code_and_no_button(page, "未设置")
    expect(block(page)).to_contain_text("FLASH_ACTIONS_URL")
