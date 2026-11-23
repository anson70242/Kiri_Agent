"""录播 page in a real browser: the automatic download box shows group files (the one being
downloaded or uploaded, and those waiting) with the group's name, the file's name and what
it is doing, apart from a live's jobs. Needs `uv run playwright install chromium`."""

import json

from .test_monitor_page import browser, page, server  # noqa: F401  (pytest fixtures)

FOLDER = "向日葵字幕组"  # invented
LIVE = {"name": "A", "platform": "youtube", "title": "歌枠", "url": "https://x/v1"}


def row(file: str, **kw) -> dict:
    return {"id": file[-6:], "group": FOLDER, "file": file} | kw


def waiting(file: str, waiting_for: str = "upload", **kw) -> dict:
    return row(file, waiting_for=waiting_for, last_error="", next_try=0) | kw


def show(page, url: str, body: dict) -> None:
    page.route("**/api/downloads", lambda route: route.fulfill(content_type="application/json", body=json.dumps(body)))
    page.goto(f"{url}/archives")
    page.locator("#downloads .job").first.wait_for()


def test_an_uploading_group_file_shows_its_group_file_and_percent(page, server):  # noqa: F811
    url, _ = server
    show(page, url, {"enabled": True, "active": [], "queue": [], "group_files": {
        "active": [row("【熟肉】第1话.mp4", doing="upload", done=25, total=100)],
        "queue": [waiting("【待校】第2话.ass")]}})

    up = page.locator("#downloads .job.group-file.active")
    assert up.locator(".chip").inner_text() == "网盘上传 25.0%"
    assert up.locator(".creator").inner_text() == FOLDER
    assert up.locator(".title").inner_text() == "【熟肉】第1话.mp4"
    assert up.locator(".bar > div").get_attribute("style") == "width:25%"
    wait = page.locator("#downloads .job.group-file:not(.active)")
    assert wait.locator(".chip").inner_text() == "等待网盘上传"
    assert wait.locator(".title").inner_text() == "【待校】第2话.ass"


def test_a_downloading_group_file_says_so(page, server):  # noqa: F811
    url, _ = server
    show(page, url, {"enabled": True, "active": [], "queue": [], "group_files": {
        "active": [row("【熟肉】第1话.mp4", doing="download", done=50, total=200)], "queue": []}})

    down = page.locator("#downloads .job.group-file.active")
    assert down.locator(".chip").inner_text() == "下载中"
    assert down.locator(".bar > div").get_attribute("style") == "width:25%"


def test_a_group_file_waiting_for_a_retry_shows_its_last_error(page, server):  # noqa: F811
    url, _ = server
    show(page, url, {"enabled": True, "active": [], "queue": [], "group_files": {"active": [], "queue": [
        waiting("【待校】第2话.ass", waiting_for="download", last_error="HTTP 503", next_try=4102444800)]}})

    wait = page.locator("#downloads .job.group-file")
    assert wait.locator(".chip").inner_text() == "等待下载"
    assert wait.locator(".err").inner_text() == "HTTP 503"
    assert "下次尝试" in wait.locator(".meta").inner_text()


def test_group_files_are_drawn_beside_a_lives_jobs_not_as_them(page, server):  # noqa: F811
    url, _ = server
    show(page, url, {
        "enabled": True,
        "active": [LIVE | {"stage": "drive", "started": 0, "percent": 40.0, "downloaded": 40,
                           "total": 100, "speed": None, "eta": None}],
        "queue": [LIVE | {"title": "雑談", "url": "https://x/v2", "stage": "drive", "queued_at": 0,
                          "next_try": 0, "attempts": 0, "last_error": ""}],
        "group_files": {"active": [], "queue": [waiting("【待校】第2话.ass")]}})

    rows = page.locator("#downloads .job")
    assert rows.count() == 3
    assert [rows.nth(i).locator(".title").inner_text() for i in range(3)] == [
        "歌枠", "【待校】第2话.ass", "雑談"]  # a waiting group file goes before a waiting drive copy
    assert page.locator("#downloads .job.group-file").count() == 1


def test_without_auto_download_group_files_still_show(page, server):  # noqa: F811
    url, _ = server
    show(page, url, {"enabled": False, "active": [], "queue": [], "group_files": {
        "active": [], "queue": [waiting("【待校】第2话.ass")]}})

    assert page.locator("#downloads .job.group-file .title").inner_text() == "【待校】第2话.ass"
