"""录播 page in a real browser: the automatic download box names what each job of a live
is doing, active and waiting, for every stage of the pipeline. Needs
`uv run playwright install chromium`."""

import json

from .test_monitor_page import browser, page, server  # noqa: F401  (pytest fixtures)

LIVE = {"name": "青葉みのり", "platform": "youtube", "url": "https://x/v1",
        "title": "【Minecraft】可愛いウーパールーパーのおうち作りたい(∂ω∂)【青葉みのり/ミリプロ】"}
NO_PROGRESS = {"percent": None, "downloaded": 0, "total": None, "speed": None, "eta": None}


def active(stage: str, **progress) -> dict:
    return LIVE | {"stage": stage, "started": 0} | NO_PROGRESS | progress


def waiting(stage: str) -> dict:
    return LIVE | {"stage": stage, "queued_at": 0, "next_try": 0, "attempts": 0, "last_error": ""}


def show(page, url: str, body: dict) -> None:
    page.route("**/api/downloads", lambda route: route.fulfill(content_type="application/json", body=json.dumps(body)))
    page.goto(f"{url}/archives")
    page.locator("#downloads .job").first.wait_for()


def chips(page, selector: str) -> list[str]:
    return page.locator(f"#downloads {selector} .chip").all_inner_texts()


def test_analysis_and_translation_of_one_live_are_two_different_rows(page, server):  # noqa: F811
    # The queue as the server held it on 2026-11-19: both jobs of one live, both running.
    url, _ = server
    show(page, url, {"enabled": True, "active": [active("analyze"), active("translate")],
                     "queue": [waiting("analyze"), waiting("translate")]})

    assert chips(page, ".job.active") == ["路灯表分析中", "中文字幕翻译中"]
    assert chips(page, ".job:not(.active)") == []
    assert page.locator("#downloads .job.active .bar").count() == 0  # no progress to show: no empty bar
    elapsed = page.locator("#downloads .job.active > .meta").all_inner_texts()
    assert len(elapsed) == 2 and all(t.startswith("已用 ") for t in elapsed)


def test_every_stage_has_its_own_label_active_and_waiting(page, server):  # noqa: F811
    url, _ = server
    stages = ["download", "transcribe", "analyze", "translate", "upload", "drive"]
    show(page, url, {"enabled": True, "active": [active(s) for s in stages],
                     "queue": [waiting(s) | {"url": "https://x/v2"} for s in stages]})

    assert chips(page, ".job.active") == [
        "下载 准备中", "转写字幕中", "路灯表分析中", "中文字幕翻译中", "上传腾讯文档中", "网盘上传 准备中"]
    assert chips(page, ".job:not(.active)") == [
        "等待下载", "等待转写", "等待路灯表分析", "等待中文字幕翻译", "等待上传腾讯文档", "等待网盘上传"]


def test_a_download_shows_its_progress(page, server):  # noqa: F811
    url, _ = server
    show(page, url, {"enabled": True, "queue": [waiting("download")],
                     "active": [active("download", percent=40.0, downloaded=40 * 2**20, total=100 * 2**20)]})

    row = page.locator("#downloads .job.active")
    assert row.locator(".chip").inner_text() == "下载中 40.0%"
    assert row.locator(".bar > div").get_attribute("style") == "width:40%"
