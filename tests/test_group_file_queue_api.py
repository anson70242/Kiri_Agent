"""GET /api/downloads with group files: the queue box's group-file rows (the job being
downloaded or uploaded, and those waiting) go only to a user who sees everything (an
admin, or login off); with no group-file jobs the answer is as before."""

from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient

from backend import auth as auth_cli
from backend.monitor.server import create_app

from .test_qq import SECRET

GROUP = "GROUP_OPENID_1"  # invented
FOLDER = "章鱼烧字幕组"
ORIGIN = {"Origin": "http://testserver"}


def write_config(tmp_path, login: bool = False):
    cli_file = tmp_path / "quark" / "quark-drive.cjs"  # the drive is there: files are kept
    cli_file.parent.mkdir()
    cli_file.write_text("// the CLI", encoding="utf-8")
    body = {
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True}],
                    "streamers_file": str(tmp_path / "data" / "streamers.json")},
        "notify": {"state_file": str(tmp_path / "state.json"), "log_file": str(tmp_path / "log.json"),
                   "qq": {"app_id_env": "KIRI_T_QQ_ID", "secret_env": "KIRI_T_QQ_SECRET",
                          "groups_file": str(tmp_path / "data" / "qq_groups.json")}},
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(tmp_path / "queue.json")}},
        "drive": {"quark_cli": str(cli_file)},
    }
    if login:
        body["auth"] = {"enabled": True}
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def make_app(tmp_path, monkeypatch, login: bool = False):
    monkeypatch.setenv("KIRI_T_QQ_ID", "APP1")
    monkeypatch.setenv("KIRI_T_QQ_SECRET", SECRET)
    cfg = write_config(tmp_path, login)
    if login:
        for argv in (["add-user", "boss", "--admin"], ["add-user", "fan"]):
            monkeypatch.setattr("sys.stdin", io.StringIO("secret-pass\n"))
            assert auth_cli.main(["--config", str(cfg), *argv, "--password-stdin"]) == 0
    app = create_app(cfg)
    groups = app.state.notifier.qq.groups
    groups.register(GROUP)
    groups.update(GROUP, name=FOLDER, group_files_to_drive=True)
    return app


def post(app, name: str, size: int = 3) -> None:
    """A member posts a file in the group; the gateway hands it to the module."""
    app.state.group_files.posted(GROUP, "MSG1", "2026-11-19T05:41:00+08:00", {
        "content_type": "file", "filename": name, "size": size,
        "url": "https://grpfile.ftn.qq.com/ftn_handler/f0001"})


def logged_in(app, name: str) -> TestClient:
    client = TestClient(app, headers=ORIGIN)
    app.state.auth.fail_delay = 0
    assert client.post("/api/login", json={"username": name, "password": "secret-pass"}).status_code == 200
    return client


def test_without_login_the_waiting_group_files_are_listed(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    post(app, "【待校】第1话.ass")
    post(app, "【熟肉】第1话.mp4", size=10)

    body = TestClient(app).get("/api/downloads").json()

    assert body["active"] == [] and body["queue"] == []  # a live's jobs: none
    rows = body["group_files"]
    assert rows["active"] == []
    assert [(r["group"], r["file"], r["waiting_for"]) for r in rows["queue"]] == [
        (FOLDER, "【待校】第1话.ass", "download"), (FOLDER, "【熟肉】第1话.mp4", "download")]


def test_with_no_group_file_jobs_the_answer_is_as_before(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert TestClient(app).get("/api/downloads").json() == {"enabled": True, "active": [], "queue": []}


def test_an_admin_gets_the_group_file_rows(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, login=True)
    post(app, "【待校】第1话.ass")

    body = logged_in(app, "boss").get("/api/downloads").json()

    assert [r["file"] for r in body["group_files"]["queue"]] == ["【待校】第1话.ass"]


def test_a_member_limited_to_some_streamers_gets_none(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, login=True)
    app.state.auth.users.update("fan", streamers=["A"])
    post(app, "【待校】第1话.ass")

    body = logged_in(app, "fan").get("/api/downloads").json()

    assert "group_files" not in body
    assert body == {"enabled": True, "active": [], "queue": []}


def test_a_member_who_sees_every_streamer_gets_none_either(tmp_path, monkeypatch):
    # A group's file names are for the owner only (the spec's user story 44): a member's
    # "all" is about streamers, not about the QQ groups.
    app = make_app(tmp_path, monkeypatch, login=True)
    app.state.auth.users.update("fan", streamers="all")
    post(app, "【待校】第1话.ass")

    body = logged_in(app, "fan").get("/api/downloads").json()

    assert "group_files" not in body
