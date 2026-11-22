"""A tab of a Tencent Docs sheet, from the address a browser shows (docs.qq.com/sheet/<file id>?tab=<tab id>),
and back to the address the 通知 page shows. Made-up ids throughout."""

from __future__ import annotations

import pytest

from backend.tencent_docs import parse_sheet_tab, sheet_tab_url


@pytest.mark.parametrize("text", [
    "https://docs.qq.com/sheet/DFakeFile123?tab=TAB0x1",
    "  https://docs.qq.com/sheet/DFakeFile123?tab=TAB0x1  ",
    "http://docs.qq.com/sheet/DFakeFile123?tab=TAB0x1",
    "docs.qq.com/sheet/DFakeFile123?tab=TAB0x1",
    "https://docs.qq.com/sheet/DFakeFile123?u=0123abcdef&tab=TAB0x1",  # logged in: the account comes first
    "https://docs.qq.com/sheet/DFakeFile123?tab=TAB0x1&_t=1763800000000",
    "https://docs.qq.com/sheet/DFakeFile123?tab=TAB0x1#gid=1",
    "https://docs.qq.com/sheet/DFakeFile123/?tab=TAB0x1",
])
def test_an_address_with_a_tab_gives_the_file_and_the_tab(text):
    assert parse_sheet_tab(text) == ("DFakeFile123", "TAB0x1")


@pytest.mark.parametrize("text", [
    "https://docs.qq.com/sheet/DFakeFile123",
    "https://docs.qq.com/sheet/DFakeFile123?u=0123abcdef",
    "https://docs.qq.com/sheet/DFakeFile123?tab=",
    "DFakeFile123",  # a bare file id names no tab either
])
def test_an_address_without_a_tab_is_refused_saying_a_tab_is_needed(text):
    with pytest.raises(ValueError, match="tab="):
        parse_sheet_tab(text)


@pytest.mark.parametrize("text", [
    "https://docs.qq.com/doc/DFakeFile123?tab=TAB0x1",  # a document, not a sheet
    "https://docs.qq.com/smartsheet/DFakeFile123?tab=TAB0x1",
    "https://example.com/sheet/DFakeFile123?tab=TAB0x1",
    "not an address",
])
def test_an_address_that_is_not_a_tencent_docs_sheet_is_refused_saying_so(text):
    with pytest.raises(ValueError, match="不是腾讯文档表格"):
        parse_sheet_tab(text)


def test_the_saved_pair_gives_back_an_address_that_parses_to_it():
    url = sheet_tab_url("DFakeFile123", "TAB0x1")
    assert url == "https://docs.qq.com/sheet/DFakeFile123?tab=TAB0x1"
    assert parse_sheet_tab(url) == ("DFakeFile123", "TAB0x1")
