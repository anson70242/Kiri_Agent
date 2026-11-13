"""直播状态 page in a real browser: a TwitCasting streamer is added from the form and
its platform tag has its own colour. Needs `uv run playwright install chromium`."""

from .test_monitor_page import browser, page, roster, server  # noqa: F401  (pytest fixtures)


def tag_colour(page, platform):  # noqa: F811
    """The text colour of the platform tag on that platform's first card."""
    return page.locator(f".card .platform.{platform}").first.evaluate("e => getComputedStyle(e).color")


def test_add_twitcasting_streamer_from_the_form(page, server):  # noqa: F811
    url, cfg = server
    page.goto(url)
    page.get_by_role("button", name="新增主播").click()
    dlg = page.get_by_role("dialog")
    dlg.get_by_label("名字").fill("C")
    dlg.get_by_label("平台").select_option(label="TwitCasting")
    dlg.get_by_label("频道").fill("https://twitcasting.tv/c:mika")
    dlg.get_by_role("button", name="添加").click()

    page.get_by_role("button", name="设置 C").wait_for()
    assert "twitcasting:https://twitcasting.tv/c:mika" in roster(cfg)

    plain = page.locator(".card .platform").first.evaluate(
        "e => { const s = document.createElement('span'); s.className = 'platform';"
        " e.parentNode.append(s); const c = getComputedStyle(s).color; s.remove(); return c; }")
    colours = {p: tag_colour(page, p) for p in ("youtube", "twitch", "twitcasting")}
    assert colours["twitcasting"] != plain
    assert colours["twitcasting"] not in (colours["youtube"], colours["twitch"])
