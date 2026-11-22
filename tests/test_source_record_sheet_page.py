"""通知 page in a real browser: each group's 「录播源记录表」 field with its small text, saved and
shown again, a refused address with the reason, and the field shown as unavailable when the
service has no Tencent Docs token. Made-up ids. Needs `uv run playwright install chromium`."""

import json

import pytest

from .test_monitor_page import browser, notify_server, page  # noqa: F401  (pytest fixtures)

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

CARD = "QQ 群 G1ABCDEF"
FIELD = "录播源记录表"
ADDRESS = "https://docs.qq.com/sheet/DFakeFile123?tab=TAB0x1"


@pytest.fixture
def token(monkeypatch):
    """A Tencent Docs token in the environment; list it before notify_server, which builds the app."""
    monkeypatch.setenv("TENCENT_DOCS_MCP_TOKEN", "made-up-token")


def saved(tmp):
    (g,) = json.loads((tmp / "qq_groups.json").read_text(encoding="utf-8"))
    return g


def test_a_sheet_is_saved_and_shown_again(token, page, notify_server):  # noqa: F811
    url, tmp = notify_server
    page.goto(url + "/notify")
    card = page.get_by_role("group", name=CARD)
    expect(card).to_contain_text("录播和网盘的链接会自动登记到这张腾讯文档")
    field = card.get_by_label(FIELD)
    expect(field).to_have_value("")

    field.fill("https://docs.qq.com/sheet/DFakeFile123?u=0123abcdef&tab=TAB0x1")
    card.get_by_role("button", name="保存").click()
    expect(page.locator("body")).to_contain_text("已保存")
    assert saved(tmp)["source_record_sheet"] == {"file_id": "DFakeFile123", "tab_id": "TAB0x1"}

    page.reload()
    expect(page.get_by_role("group", name=CARD).get_by_label(FIELD)).to_have_value(ADDRESS)


def test_an_address_without_a_tab_is_refused_with_the_reason(token, page, notify_server):  # noqa: F811
    url, tmp = notify_server
    page.goto(url + "/notify")
    card = page.get_by_role("group", name=CARD)
    card.get_by_label(FIELD).fill("https://docs.qq.com/sheet/DFakeFile123")
    card.get_by_role("button", name="保存").click()
    expect(card).to_contain_text("tab=")
    assert "source_record_sheet" not in saved(tmp)


def test_without_a_token_the_field_is_unavailable_and_the_rest_still_saves(page, notify_server):  # noqa: F811
    url, tmp = notify_server

    def no_token(route):  # the status call as on a server without the token
        route.fulfill(json={**route.fetch().json(), "tencent_docs_available": False})
    page.route("**/api/notify", no_token)
    page.goto(url + "/notify")
    card = page.get_by_role("group", name=CARD)
    expect(card).to_contain_text("没有腾讯文档的令牌")
    expect(card.get_by_label(FIELD)).to_be_disabled()

    card.get_by_label("群名").fill("章鱼烧字幕组")
    card.get_by_role("button", name="保存").click()
    expect(page.locator("body")).to_contain_text("已保存")
    assert saved(tmp)["name"] == "章鱼烧字幕组" and "source_record_sheet" not in saved(tmp)
