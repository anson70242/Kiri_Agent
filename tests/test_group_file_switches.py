"""The two group-file switches of a QQ group on the 通知 page: "群文件上传到夸克网盘" and
"群文件上传通知", saved with the group, off until the owner turns them on, and the first one
refused while the group's name cannot be its group folder."""

from __future__ import annotations

import json

import pytest

from backend.state_file import StateFileError

from . import test_notify_api as notify_test
from .test_notify_api import app, write_config


def patch(client, body, gid="G1"):
    return client.patch("/api/notify/groups", params={"id": gid}, json=body)


def group(client):
    (g,) = client.get("/api/notify").json()["groups"]
    return g


def saved(tmp_path):
    (g,) = json.loads((tmp_path / "qq_groups.json").read_text(encoding="utf-8"))
    return g


def test_a_new_group_starts_with_both_switches_off(tmp_path, monkeypatch):
    g = group(app(tmp_path, monkeypatch))
    assert (g["group_files_to_drive"], g["group_file_notices"]) == (False, False)
    assert g["group_folder"] == ""  # no name yet


def test_a_group_saved_before_the_switches_reads_as_off_and_off(tmp_path, monkeypatch):
    (tmp_path / "qq_groups.json").write_text(json.dumps([
        {"openid": "G1", "name": "章鱼烧字幕组", "enabled": True, "streamers": ["A"], "kinds": ["live"],
         "status": "active", "added_at": "2026-11-01T00:00:00+00:00"}]), encoding="utf-8")
    client = app(tmp_path, monkeypatch)  # registering a known group changes nothing

    g = group(client)
    assert (g["group_files_to_drive"], g["group_file_notices"]) == (False, False)
    assert g["group_folder"] == "章鱼烧字幕组"
    files = client.app.state.notifier.qq.groups.group_files("G1")
    assert (files.to_drive, files.notices, files.folder) == (False, False, "章鱼烧字幕组")


def test_both_switches_are_saved_and_read_back(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    r = patch(client, {"name": "章鱼烧字幕组", "group_files_to_drive": True, "group_file_notices": True})
    assert r.status_code == 200 and r.json()["group_files_to_drive"] is True

    g = group(client)
    assert (g["group_files_to_drive"], g["group_file_notices"], g["group_folder"]) == (True, True, "章鱼烧字幕组")
    assert (saved(tmp_path)["group_files_to_drive"], saved(tmp_path)["group_file_notices"]) == (True, True)
    files = client.app.state.notifier.qq.groups.group_files("G1")  # what the group-files module reads
    assert (files.to_drive, files.notices, files.folder) == (True, True, "章鱼烧字幕组")

    assert patch(client, {"group_files_to_drive": False}).status_code == 200
    assert (group(client)["group_files_to_drive"], group(client)["group_file_notices"]) == (False, True)


def test_the_notice_switch_can_be_on_without_a_name_or_the_first_switch(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    assert patch(client, {"group_file_notices": True}).status_code == 200
    assert group(client)["group_file_notices"] is True


def test_turning_the_first_switch_on_without_a_name_is_refused(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    for body in ({"group_files_to_drive": True}, {"name": "   ", "group_files_to_drive": True}):
        r = patch(client, body)
        assert r.status_code == 422 and "先填群名" in r.json()["detail"]
    assert saved(tmp_path).get("group_files_to_drive") is False


def test_a_name_that_cannot_be_one_folder_is_refused_with_what_is_wrong(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    for name, says in [("字幕组/二组", "/"), ("字幕组\\二组", "\\"), ("字幕组:二组", ":"), ("字幕组?", "?"),
                       ("..", ".."), ("<字幕组>", "<")]:
        r = patch(client, {"name": name, "group_files_to_drive": True})
        assert r.status_code == 422, name
        assert "群名不能当网盘文件夹名" in r.json()["detail"] and says in r.json()["detail"], name
    assert (saved(tmp_path)["name"], saved(tmp_path)["group_files_to_drive"]) == ("", False)  # nothing saved


def test_while_the_first_switch_is_on_the_name_cannot_be_cleared_or_spoiled(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    assert patch(client, {"name": "章鱼烧字幕组", "group_files_to_drive": True}).status_code == 200

    r = patch(client, {"name": ""})
    assert r.status_code == 422 and "先填群名" in r.json()["detail"]
    r = patch(client, {"name": "章鱼烧|字幕组"})
    assert r.status_code == 422 and "|" in r.json()["detail"]
    assert saved(tmp_path)["name"] == "章鱼烧字幕组"

    assert patch(client, {"name": "  章鱼烧二组 "}).status_code == 200  # a usable name: the folder follows
    assert group(client)["group_folder"] == "章鱼烧二组"
    assert patch(client, {"name": "", "group_files_to_drive": False}).status_code == 200  # off, then cleared


def test_the_switches_do_not_depend_on_notices_streamers_or_kinds(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    assert patch(client, {"name": "章鱼烧字幕组", "enabled": False, "streamers": [], "kinds": [],
                          "group_files_to_drive": True, "group_file_notices": True}).status_code == 200
    assert patch(client, {"enabled": True, "streamers": ["A", "B"], "kinds": ["live"]}).status_code == 200
    assert patch(client, {"enabled": False, "streamers": [], "kinds": []}).status_code == 200

    g = group(client)
    assert (g["group_files_to_drive"], g["group_file_notices"]) == (True, True)
    files = client.app.state.notifier.qq.groups.group_files("G1")
    assert (files.to_drive, files.notices) == (True, True)


def with_drive(tmp_path, cli: bool):
    """The notify config, with the drive's CLI in place or missing."""
    cfg = write_config(tmp_path)
    body = json.loads(cfg.read_text(encoding="utf-8"))
    cli_file = tmp_path / "quark" / "quark-drive.cjs"
    if cli:
        cli_file.parent.mkdir()
        cli_file.write_text("// the CLI", encoding="utf-8")
    body["drive"] = {"quark_cli": str(cli_file)}
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


@pytest.mark.parametrize("cli", [True, False])
def test_the_status_says_once_whether_the_drive_is_available(tmp_path, monkeypatch, cli):
    monkeypatch.setattr(notify_test, "write_config", lambda _tmp: with_drive(tmp_path, cli))
    body = app(tmp_path, monkeypatch).get("/api/notify").json()
    assert body["drive_available"] is cli
    assert "drive_available" not in body["groups"][0]


@pytest.mark.parametrize("switches", [{"group_files_to_drive": "yes"}, {"group_file_notices": 1},
                                      {"group_files_to_drive": None}])
def test_a_groups_file_with_a_switch_that_is_not_true_or_false_stops_the_service(tmp_path, monkeypatch, switches):
    text = json.dumps([{"openid": "G1", "name": "章鱼烧字幕组", **switches}])
    (tmp_path / "qq_groups.json").write_text(text, encoding="utf-8")

    with pytest.raises(StateFileError) as e:
        app(tmp_path, monkeypatch)
    assert e.value.path == tmp_path / "qq_groups.json"
    assert (tmp_path / "qq_groups.json").read_text(encoding="utf-8") == text  # left alone


def test_a_removed_group_keeps_its_switches_but_its_files_are_off(tmp_path, monkeypatch):
    client = app(tmp_path, monkeypatch)
    groups = client.app.state.notifier.qq.groups
    patch(client, {"name": "章鱼烧字幕组", "group_files_to_drive": True, "group_file_notices": True})
    groups.update("G1", status="removed")  # the bot was kicked

    assert (group(client)["group_files_to_drive"], group(client)["group_file_notices"]) == (True, True)
    files = groups.group_files("G1")
    assert (files.to_drive, files.notices, files.folder) == (False, False, "章鱼烧字幕组")
    assert groups.group_files("NOPE") is None
