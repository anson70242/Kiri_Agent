"""录播 page in a real browser: an archive's page shows "网盘链接" next to "全部下载 (zip)"
when the archive has a drive link. Needs `uv run playwright install chromium`."""

import json
from urllib.parse import quote

from .test_monitor_page import browser, page, server  # noqa: F401  (pytest fixtures)

FOLDER = "森野鈴/20261105/歌枠"
STEM = "[20261105][森野鈴] 歌枠"
ARCHIVE = {"creator": "森野鈴", "date": "20261105", "title": "歌枠", "folder": FOLDER,
           "video": f"{STEM}.mp4", "video_size": 10, "downloading": False, "has_chat": False,
           "chat_file": None, "raw_chat_file": None, "chat_count": 0, "transcript_file": None,
           "zh_transcript_file": None, "highlights_file": None, "drive_link": None, "size": 10, "mtime": 0}
LINK = {"url": "https://pan.example/s/f00dcafe?pwd=k7q2", "code": "k7q2",
        "path": f"录播/森野鈴/{STEM}.zip", "made": "2026-11-18T12:30:00+00:00"}


def open_archive_page(page, url, archive):  # noqa: F811
    body = {"output_dir": "/dl", "disk": None, "archives": [archive]}
    page.route("**/api/archives", lambda route: route.fulfill(content_type="application/json", body=json.dumps(body)))
    page.goto(f"{url}/archives#{quote(FOLDER, safe='')}")
    page.get_by_role("link", name="⬇ 全部下载 (zip)").wait_for()


def test_an_archive_with_a_drive_link_shows_it_next_to_the_zip(page, server):  # noqa: F811
    url, _ = server
    open_archive_page(page, url, ARCHIVE | {"drive_link": LINK})

    links = page.locator(".links a")
    names = [links.nth(i).inner_text().strip() for i in range(links.count())]
    drive = page.get_by_role("link", name="网盘链接")
    assert drive.get_attribute("href") == LINK["url"]
    assert drive.get_attribute("target") == "_blank"
    assert names.index("☁ 网盘链接") == names.index("⬇ 全部下载 (zip)") + 1


def test_an_archive_without_a_drive_link_shows_no_button(page, server):  # noqa: F811
    url, _ = server
    open_archive_page(page, url, ARCHIVE)

    assert page.get_by_role("link", name="网盘链接").count() == 0
