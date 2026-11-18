"""录播 page in a real browser: the automatic download box shows a drive job, waiting and
uploading, as 网盘上传 with its progress. Needs `uv run playwright install chromium`."""

import json

from .test_monitor_page import browser, page, server  # noqa: F401  (pytest fixtures)

ROW = {"name": "A", "platform": "youtube", "title": "歌枠", "url": "https://x/v1"}


def test_the_queue_view_shows_drive_jobs_as_drive_uploads(page, server):  # noqa: F811
    url, _ = server
    body = {"enabled": True,
            "active": [ROW | {"stage": "drive", "started": 0, "percent": 25.0, "downloaded": 50 * 2**20,
                              "total": 200 * 2**20, "speed": None, "eta": None}],
            "queue": [ROW | {"stage": "drive", "queued_at": 0, "next_try": 0, "attempts": 0, "last_error": ""},
                      ROW | {"title": "雑談", "url": "https://x/v2", "stage": "drive", "queued_at": 0,
                             "next_try": 0, "attempts": 0, "last_error": ""}]}
    page.route("**/api/downloads", lambda route: route.fulfill(content_type="application/json", body=json.dumps(body)))
    page.goto(f"{url}/archives")

    active = page.locator("#downloads .job.active")
    active.wait_for()
    assert active.locator(".chip").inner_text() == "网盘上传 25.0%"
    assert active.locator(".bar > div").get_attribute("style") == "width:25%"
    waiting = page.locator("#downloads .job:not(.active)")
    assert waiting.count() == 1
    assert waiting.locator(".chip").inner_text() == "等待网盘上传"
    assert waiting.locator(".title").inner_text() == "雑談"
