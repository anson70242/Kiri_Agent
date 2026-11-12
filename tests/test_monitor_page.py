"""直播状态 page in a real browser: add a streamer, the gear, delete.

Runs the real app (with a fake channel check and no background polling) on a free
port and drives it with Playwright. Needs `uv run playwright install chromium`.
"""

import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from backend.monitor.server import create_app

from .test_streamers_api import channel_exists, write_config

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    # Own start/stop (not pytest-playwright's session-wide one): Playwright's event
    # loop must be gone before other tests call asyncio.run().
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except sync_api.Error as e:
            pytest.skip(f"no Chromium (uv run playwright install chromium): {str(e).splitlines()[0]}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.set_default_timeout(5000)
    yield pg
    ctx.close()


@pytest.fixture
def server(tmp_path):
    cfg = write_config(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # lifespan off: no polling of the (fake) channels in the background.
    srv = uvicorn.Server(uvicorn.Config(create_app(cfg, check_channel=channel_exists),
                                        host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", cfg
    srv.should_exit = True
    t.join(5)


def roster(cfg):
    """What the server has saved, read back by a fresh app (as after a restart)."""
    return {s["id"]: s for s in TestClient(create_app(cfg)).get("/api/streamers").json()["streamers"]}


def test_add_streamer(page, server):
    url, cfg = server
    page.goto(url)
    page.get_by_role("button", name="新增主播").click()
    dlg = page.get_by_role("dialog")
    dlg.get_by_label("名字").fill("C")
    dlg.get_by_label("平台").select_option("youtube")
    dlg.get_by_label("频道").fill("@missing")
    dlg.get_by_role("button", name="添加").click()
    expect_text(dlg, "找不到这个频道")  # stays open with the reason

    dlg.get_by_label("频道").fill("@c")
    dlg.get_by_role("button", name="添加").click()

    page.get_by_role("button", name="设置 C").wait_for()  # its card is on the page
    assert "youtube:@c" in roster(cfg)


def test_gear_turns_auto_download_on(page, server):
    url, cfg = server
    page.goto(url)
    page.get_by_role("button", name="设置 B").click()
    dlg = page.get_by_role("dialog")
    dlg.get_by_label("自动下载").check()
    dlg.get_by_label("保留录播数").fill("3")
    dlg.get_by_role("button", name="保存").click()
    dlg.wait_for(state="hidden")

    b = roster(cfg)["twitch:b"]
    assert (b["auto_download"], b["keep"]) == (True, 3)


def test_delete_asks_first(page, server):
    url, cfg = server
    page.goto(url)

    page.once("dialog", lambda d: d.dismiss())  # cancel the confirm(): nothing happens
    page.get_by_role("button", name="设置 A").click()
    page.get_by_role("dialog").get_by_role("button", name="删除主播").click()
    assert "youtube:@a" in roster(cfg)

    page.once("dialog", lambda d: d.accept())
    page.get_by_role("dialog").get_by_role("button", name="删除主播").click()
    page.get_by_role("button", name="设置 A").wait_for(state="detached")
    assert "youtube:@a" not in roster(cfg)


def expect_text(locator, text, timeout=5.0):
    end = time.time() + timeout
    while text not in locator.inner_text():
        assert time.time() < end, f"{text!r} never appeared"
        time.sleep(0.05)
