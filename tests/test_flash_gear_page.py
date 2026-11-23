"""The gear's "自动上传到 QQ 闪传" on the 直播状态 page, in a real browser (skipped without
Chromium): the box, its greyed state without a flash service, and the card's mark."""

from .test_flash_gear import flash_off, flash_on  # noqa: F401 (fixtures)
from .test_monitor_page import browser, expect_text, page, roster, server  # noqa: F401 (fixtures)


def test_gear_ticks_the_flash_box_and_the_card_shows_it(page, server, flash_on):
    url, cfg = server
    page.goto(url)
    page.get_by_role("button", name="设置 A").click()
    dlg = page.get_by_role("dialog")
    box = dlg.get_by_label("自动上传到 QQ 闪传")
    assert box.is_enabled() and not box.is_checked()
    box.check()
    dlg.get_by_role("button", name="保存").click()
    dlg.wait_for(state="hidden")

    a = roster(cfg)["youtube:@a"]
    assert (a["flash"], a["drive"]) == (True, False)
    expect_text(page.locator("body"), "⚡ 闪传")


def test_without_a_flash_service_the_box_is_greyed_out_with_the_reason(page, server, flash_off):
    url, cfg = server
    page.goto(url)
    page.get_by_role("button", name="设置 A").click()
    dlg = page.get_by_role("dialog")

    assert dlg.get_by_label("自动上传到 QQ 闪传").is_disabled()
    expect_text(dlg, "未设置闪传服务")
    dlg.get_by_role("button", name="保存").click()  # the rest of the gear still saves
    dlg.wait_for(state="hidden")
    assert roster(cfg)["youtube:@a"]["flash"] is False
    assert "闪传" not in page.locator("#grid").inner_text()
