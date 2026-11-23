"""录播 page in a real browser: an archive's page shows "闪传链接" beside "网盘链接" when the
archives API gives a flash link. Needs `uv run playwright install chromium`."""

from .test_drive_link_page import ARCHIVE, LINK as DRIVE_LINK, open_archive_page
from .test_monitor_page import browser, page, server  # noqa: F401  (pytest fixtures)

FLASH_LINK = {"url": "https://flash.example/q/MadeUp7f3a", "expires": "2026-12-07T18:30:00+00:00",
              "made": "2026-11-23T12:30:00+00:00"}


def link_names(page):  # noqa: F811
    links = page.locator(".links a")
    return [links.nth(i).inner_text().strip() for i in range(links.count())]


def test_an_archive_with_a_flash_link_shows_it_beside_the_drive_link(page, server):  # noqa: F811
    url, _ = server
    open_archive_page(page, url, ARCHIVE | {"drive_link": DRIVE_LINK, "flash_link": FLASH_LINK})

    flash = page.get_by_role("link", name="闪传链接")
    assert flash.get_attribute("href") == FLASH_LINK["url"]
    assert flash.get_attribute("target") == "_blank"
    assert flash.get_attribute("title") == "有效至 2026/12/8"  # its day in Hong Kong (UTC+8), as the sheet writes dates
    names = link_names(page)
    assert names.index("⚡ 闪传链接") == names.index("☁ 网盘链接") + 1
    assert "⬇ 全部下载 (zip)" in names  # the page's own zip stays


def test_an_archive_with_only_a_flash_link_shows_it_after_the_zip(page, server):  # noqa: F811
    url, _ = server
    open_archive_page(page, url, ARCHIVE | {"flash_link": FLASH_LINK})

    names = link_names(page)
    assert names.index("⚡ 闪传链接") == names.index("⬇ 全部下载 (zip)") + 1


def test_an_archive_without_a_flash_link_shows_no_button(page, server):  # noqa: F811
    url, _ = server
    open_archive_page(page, url, ARCHIVE | {"flash_link": None})

    assert page.get_by_role("link", name="闪传链接").count() == 0
