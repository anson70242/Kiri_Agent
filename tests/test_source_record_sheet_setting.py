"""A QQ group's source record sheet (录播源记录表) on the 通知 page: the address of one tab of a
Tencent Docs sheet, saved with the group's other settings as its file id and tab id, shown again
as an address, cleared by an empty field, and refused while the service has no Tencent Docs
token. Made-up file ids, tab ids and group ids throughout."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.monitor.server import create_app

from . import test_notify_api as notify_test
from .test_notify_api import write_config

TOKEN_ENV = "KIRI_T_TENCENT"
ADDRESS = "https://docs.qq.com/sheet/DFakeFile123?tab=TAB0x1"


def config(tmp_path):
    """The notify tests' config, with the Tencent Docs token under a name of the tests' own."""
    cfg = write_config(tmp_path)
    body = json.loads(cfg.read_text(encoding="utf-8"))
    body["upload"] = {"tencent": {"token_env": TOKEN_ENV}}
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def app(tmp_path, monkeypatch, token=True):
    if token:
        monkeypatch.setenv(TOKEN_ENV, "made-up-token")
    else:
        monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setattr(notify_test, "write_config", config)
    return notify_test.app(tmp_path, monkeypatch)


def patch(client, body, gid="G1"):
    return client.patch("/api/notify/groups", params={"id": gid}, json=body)


def group(client):
    (g,) = client.get("/api/notify").json()["groups"]
    return g


def saved(tmp_path):
    (g,) = json.loads((tmp_path / "qq_groups.json").read_text(encoding="utf-8"))
    return g


def test_a_sheet_is_saved_as_its_file_and_tab_and_shown_again_after_a_restart(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    r = patch(client, {"source_record_sheet": "https://docs.qq.com/sheet/DFakeFile123?u=0123abcdef&tab=TAB0x1"})
    assert r.status_code == 200 and r.json()["source_record_sheet"] == ADDRESS

    assert saved(tmp_path)["source_record_sheet"] == {"file_id": "DFakeFile123", "tab_id": "TAB0x1"}
    restarted = TestClient(create_app(config(tmp_path)))
    assert group(restarted)["source_record_sheet"] == ADDRESS


def test_a_new_group_and_a_group_saved_before_the_field_have_no_sheet(tmp_path, monkeypatch):
    (tmp_path / "qq_groups.json").write_text(json.dumps([
        {"openid": "G1", "name": "章鱼烧字幕组", "enabled": True, "streamers": ["A"], "kinds": ["live"],
         "status": "active", "added_at": "2026-11-01T00:00:00+00:00"}]), encoding="utf-8")
    client = app(tmp_path, monkeypatch)
    assert group(client)["source_record_sheet"] == ""

    assert patch(client, {"enabled": False}).status_code == 200  # other settings saved: still no key
    assert "source_record_sheet" not in saved(tmp_path)


def test_an_address_without_a_tab_is_refused_and_nothing_is_saved(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    r = patch(client, {"name": "章鱼烧字幕组", "source_record_sheet": "https://docs.qq.com/sheet/DFakeFile123"})
    assert r.status_code == 422 and "tab=" in r.json()["detail"]
    assert saved(tmp_path)["name"] == "" and "source_record_sheet" not in saved(tmp_path)


def test_an_address_that_is_not_a_sheet_is_refused_and_the_saved_one_stays(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    assert patch(client, {"source_record_sheet": ADDRESS}).status_code == 200
    for address in ("https://docs.qq.com/doc/DFakeFile456?tab=TAB0x2", "https://example.com/sheet/x?tab=y"):
        r = patch(client, {"source_record_sheet": address})
        assert r.status_code == 422 and "不是腾讯文档表格" in r.json()["detail"], address
    assert group(client)["source_record_sheet"] == ADDRESS


def test_an_empty_field_clears_the_sheet_and_leaving_it_out_keeps_it(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    assert patch(client, {"source_record_sheet": ADDRESS}).status_code == 200
    assert patch(client, {"name": "章鱼烧字幕组"}).status_code == 200
    assert group(client)["source_record_sheet"] == ADDRESS

    assert patch(client, {"source_record_sheet": "  "}).status_code == 200
    assert group(client)["source_record_sheet"] == ""
    assert "source_record_sheet" not in saved(tmp_path) and saved(tmp_path)["name"] == "章鱼烧字幕组"


def test_two_groups_name_two_sheets(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    client.app.state.notifier.qq.groups.register("G2")
    other = "https://docs.qq.com/sheet/DFakeFile456?tab=TAB0x2"
    assert patch(client, {"source_record_sheet": ADDRESS}).status_code == 200
    assert patch(client, {"source_record_sheet": other}, gid="G2").status_code == 200
    groups = {g["openid"]: g["source_record_sheet"] for g in client.get("/api/notify").json()["groups"]}
    assert groups == {"G1": ADDRESS, "G2": other}


def test_without_a_token_the_page_is_told_and_a_sheet_is_refused_but_can_be_cleared(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch, token=False)
    assert client.get("/api/notify").json()["tencent_docs_available"] is False
    r = patch(client, {"source_record_sheet": ADDRESS})
    assert r.status_code == 422 and "没有腾讯文档的令牌" in r.json()["detail"]
    assert "source_record_sheet" not in saved(tmp_path)
    assert patch(client, {"source_record_sheet": ""}).status_code == 200


def test_with_a_token_the_page_is_told_the_field_can_be_used(tmp_path, monkeypatch):
    assert app(tmp_path, monkeypatch).get("/api/notify").json()["tencent_docs_available"] is True


def test_only_an_admin_reads_or_changes_the_sheet(tmp_path, monkeypatch):
    from .test_auth import login, make_client

    client = make_client(tmp_path, monkeypatch)
    login(client, "fan")
    for r in (client.get("/api/notify"),
              client.patch("/api/notify/groups", params={"id": "G1"}, json={"source_record_sheet": ADDRESS})):
        assert r.status_code == 403, r.request.url
