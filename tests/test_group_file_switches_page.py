"""通知 page in a real browser: each group's two group-file switches, the group folder next to
the first, the hint about "接收所有消息", and the line in its place when the drive is not
available. Needs `uv run playwright install chromium`."""

import json

import pytest

from .test_monitor_page import browser, notify_server, page  # noqa: F401  (pytest fixtures)

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

CARD = "QQ 群 G1ABCDEF"


def saved(tmp):
    (g,) = json.loads((tmp / "qq_groups.json").read_text(encoding="utf-8"))
    return g


def drive_available(page):
    """The status call as on a server with the drive."""
    def answer(route):
        body = route.fetch().json()
        route.fulfill(json={**body, "drive_available": True})
    page.route("**/api/notify", answer)


def test_both_switches_are_off_and_saving_sends_them(page, notify_server):  # noqa: F811
    url, tmp = notify_server
    page.goto(url + "/notify")
    card = page.get_by_role("group", name=CARD)
    first, second = card.get_by_label("群文件上传到夸克网盘"), card.get_by_label("群文件上传通知")
    expect(first).not_to_be_checked()
    expect(second).not_to_be_checked()

    card.get_by_label("群名").fill("向日葵字幕组")
    first.check()
    second.check()
    card.get_by_role("button", name="保存").click()
    expect(page.locator("body")).to_contain_text("已保存")

    g = saved(tmp)
    assert (g["group_files_to_drive"], g["group_file_notices"], g["enabled"]) == (True, True, False)
    card = page.get_by_role("group", name="QQ 群 向日葵字幕组")
    expect(card).to_contain_text("网盘位置：向日葵字幕组/")
    expect(card.get_by_label("群文件上传到夸克网盘")).to_be_checked()


def test_without_the_drive_the_page_says_so_in_place_of_the_hint(page, notify_server):  # noqa: F811
    url, _ = notify_server  # no Quark CLI on this test server
    page.goto(url + "/notify")
    card = page.get_by_role("group", name=CARD)
    expect(card).to_contain_text("这台服务器上没有夸克网盘")
    expect(card).not_to_contain_text("接收所有消息")


def test_with_the_drive_the_page_shows_the_folder_and_the_hint(page, notify_server):  # noqa: F811
    url, _ = notify_server
    drive_available(page)
    page.goto(url + "/notify")
    card = page.get_by_role("group", name=CARD)
    expect(card).to_contain_text("接收所有消息")
    expect(card).not_to_contain_text("这台服务器上没有夸克网盘")
    expect(card).to_contain_text("网盘位置：先填群名")  # no name yet: no folder


def test_a_refused_switch_shows_why(page, notify_server):  # noqa: F811
    url, tmp = notify_server
    page.goto(url + "/notify")
    card = page.get_by_role("group", name=CARD)
    card.get_by_label("群文件上传到夸克网盘").check()
    card.get_by_role("button", name="保存").click()
    expect(card).to_contain_text("先填群名")
    assert saved(tmp)["group_files_to_drive"] is False
