"""Login in a real browser: what members and admins see, the 用户 page, 登出, next=.

Same pattern as test_monitor_page.py (real app on a free port, Playwright), with login on.
"""

import threading
import time

import pytest
import uvicorn

from backend.auth import load_auth
from backend.monitor.server import create_app

from .helpers import free_port
from .test_auth import cli, write_config
from .test_monitor_page import browser, page  # noqa: F401  (fixtures)

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect


@pytest.fixture
def server(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    assert cli(monkeypatch, cfg, "add-user", "boss", "--admin", "--password-stdin") == 0
    assert cli(monkeypatch, cfg, "add-user", "fan", "--password-stdin") == 0
    load_auth(cfg).users.update("fan", streamers="all")  # new members see nobody until picked
    app = create_app(cfg)
    app.state.auth.fail_delay = 0
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", cfg
    srv.should_exit = True
    t.join(5)


def log_in(page, name, password="secret-pass"):
    page.get_by_label("用户名").fill(name)
    page.get_by_label("密码").fill(password)
    page.get_by_role("button", name="登录").click()


def nav(page, name):
    return page.locator(".nav").get_by_role("link", name=name, exact=True)


def test_member_sees_no_admin_controls(page, server):
    url, _ = server
    page.goto(url)
    page.wait_for_url("**/login?next=%2F")
    log_in(page, "fan")
    page.wait_for_url(url + "/")
    expect(page.locator(".user-box")).to_contain_text("fan")
    page.locator(".card").first.wait_for()
    expect(page.locator(".gear")).to_have_count(1)  # in the page, but not shown
    expect(page.locator(".gear")).to_be_hidden()
    expect(page.locator("#add")).to_be_hidden()
    expect(nav(page, "通知")).to_be_hidden()
    expect(nav(page, "用户")).to_be_hidden()
    expect(nav(page, "录播")).to_be_visible()


def test_admin_sees_everything_and_adds_a_member(page, server):
    url, cfg = server
    page.goto(url + "/login")
    log_in(page, "boss")
    page.wait_for_url(url + "/")
    expect(page.get_by_role("button", name="设置 A")).to_be_visible()
    expect(page.get_by_role("button", name="新增主播")).to_be_visible()
    expect(nav(page, "通知")).to_be_visible()

    nav(page, "用户").click()
    page.wait_for_url(url + "/users")
    me = page.get_by_role("row").filter(has_text="boss")
    expect(me).to_contain_text("（你）")
    expect(me.get_by_role("button")).to_have_count(1)  # only 重设密码: no demote/disable/delete of yourself

    page.get_by_role("button", name="新增用户").click()
    dlg = page.get_by_role("dialog")
    dlg.get_by_label("用户名").fill("newbie")
    dlg.get_by_label("密码").fill("short")
    dlg.get_by_role("button", name="添加").click()
    expect(dlg).to_contain_text("密码至少 8 位")  # the server's reason, inline

    dlg.get_by_label("密码").fill("newbie-pass")
    dlg.get_by_label("角色").select_option(label="成员")
    dlg.get_by_role("button", name="添加").click()
    expect(page.get_by_role("row").filter(has_text="newbie")).to_contain_text("成员")
    u = load_auth(cfg).users.get("newbie")
    assert u is not None and u["role"] == "member"

    page.get_by_role("button", name="停用 newbie").click()
    expect(page.get_by_role("row").filter(has_text="newbie")).to_contain_text("已停用")
    assert load_auth(cfg).users.get("newbie")["disabled"] is True


def test_logout_returns_to_the_login_page(page, server):
    url, _ = server
    page.goto(url + "/login")
    log_in(page, "fan")
    page.wait_for_url(url + "/")
    page.get_by_role("button", name="登出").click()
    page.wait_for_url(url + "/login")
    page.goto(url + "/archives")  # really logged out
    page.wait_for_url("**/login?next=%2Farchives")


def test_login_returns_to_the_page_you_wanted(page, server):
    url, _ = server
    page.goto(url + "/highlights?x=1")
    page.wait_for_url("**/login?next=%2Fhighlights%3Fx%3D1")
    log_in(page, "fan")
    page.wait_for_url(url + "/highlights?x=1")
    expect(page.locator(".user-box")).to_contain_text("fan")


def test_a_401_from_any_fetch_goes_to_login(page, server):
    # An already-open page whose session is gone (expired, or logged out elsewhere):
    # its next request (here one we make; the page's own polling would do the same)
    # takes the user to the login page, coming back here afterwards.
    url, _ = server
    page.goto(url + "/login?next=%2Farchives")
    log_in(page, "fan")
    page.wait_for_url(url + "/archives")
    page.context.clear_cookies()
    with page.expect_navigation(url="**/login?next=%2Farchives"):
        page.evaluate("setTimeout(() => fetch('/api/status'), 0)")  # returns before the page leaves


@pytest.fixture
def two_people(tmp_path, monkeypatch):
    from .test_streamer_access import write_config as two_config

    cfg = two_config(tmp_path)
    assert cli(monkeypatch, cfg, "add-user", "boss", "--admin", "--password-stdin") == 0
    assert cli(monkeypatch, cfg, "add-user", "fan", "--password-stdin") == 0
    app = create_app(cfg)
    app.state.auth.fail_delay = 0
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", cfg
    srv.should_exit = True
    t.join(5)


def test_admin_picks_whom_a_member_sees(page, two_people):
    url, cfg = two_people
    page.goto(url + "/login?next=%2Fusers")
    log_in(page, "boss")
    page.wait_for_url(url + "/users")
    fan = page.get_by_role("row").filter(has_text="fan")
    expect(fan).to_contain_text("（还没有）")
    fan.get_by_role("button", name="可看主播 fan").click()
    dlg = page.get_by_role("dialog")
    dlg.get_by_label("A", exact=True).check()
    dlg.get_by_role("button", name="保存").click()
    expect(fan).to_contain_text("A")
    assert load_auth(cfg).users.get("fan")["streamers"] == ["A"]

    page.get_by_role("button", name="登出").click()
    page.wait_for_url(url + "/login")
    log_in(page, "fan")
    page.wait_for_url(url + "/")
    page.locator(".card").first.wait_for()
    expect(page.locator(".card .name")).to_have_text(["A", "A"])  # A's two channels, no B
    page.goto(url + "/archives")
    expect(page.locator("body")).to_contain_text("歌枠")
    expect(page.locator("body")).not_to_contain_text("雑談")
