"""Operator data (the roster, the users, the QQ group subscriptions) damaged: the service
refuses to start with one readable message and never runs on an empty list. The sessions
are runtime state: a damaged sessions file is set aside and the site runs on."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import auth as auth_cli
from backend.monitor.server import create_app
from backend.notify import Notifier
from backend.state_file import StateFileError

from .helpers import AlertRecorder

DAMAGED = '[{"name": "A", "platform": '
REPO = Path(__file__).resolve().parents[1]


def write_config(tmp_path, auth: bool = False, qq: bool = False):
    data = tmp_path / "data"
    notify: dict = {"discord": {"enabled": False}, "state_file": str(tmp_path / "notify_state.json"),
                    "log_file": str(tmp_path / "notify_log.json")}
    if qq:
        notify["qq"] = {"app_id_env": "KIRI_T_QQ_ID", "secret_env": "KIRI_T_QQ_SECRET",
                        "groups_file": str(data / "qq_groups.json")}
    body: dict = {
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a"}],
                    "streamers_file": str(data / "streamers.json")},
        "notify": notify,
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(tmp_path / "queue.json")},
                     "chat_retry": {"state_file": str(tmp_path / "pending.json")}},
    }
    if auth:
        body["auth"] = {"enabled": True, "users_file": str(data / "users.json"),
                        "sessions_file": str(data / "sessions.json")}
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def damage(path, text: str = DAMAGED):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---- the roster ----

@pytest.mark.parametrize("text", [DAMAGED, '{"name": "A"}', '[{"name": "A"}]'])
def test_a_damaged_roster_stops_create_app_and_is_left_alone(tmp_path, text):
    roster = damage(tmp_path / "data" / "streamers.json", text)

    with pytest.raises(StateFileError) as e:
        create_app(write_config(tmp_path))
    assert e.value.path == roster
    assert roster.read_text(encoding="utf-8") == text  # never replaced by the seed


# ---- the users ----

@pytest.mark.parametrize("text", [DAMAGED, '{"boss": {}}', '[{"role": "admin"}]'])
def test_a_damaged_users_file_stops_create_app(tmp_path, text):
    users = damage(tmp_path / "data" / "users.json", text)

    with pytest.raises(StateFileError) as e:
        create_app(write_config(tmp_path, auth=True))
    assert e.value.path == users
    assert users.read_text(encoding="utf-8") == text


# ---- the QQ group subscriptions ----

@pytest.mark.parametrize("text", [DAMAGED, '{"g1": {}}', '[{"name": "no openid"}]'])
def test_damaged_qq_groups_stop_create_app(tmp_path, monkeypatch, text):
    monkeypatch.setenv("KIRI_T_QQ_ID", "APP1")
    monkeypatch.setenv("KIRI_T_QQ_SECRET", "s3cret")
    groups = damage(tmp_path / "data" / "qq_groups.json", text)

    with pytest.raises(StateFileError) as e:
        create_app(write_config(tmp_path, qq=True))
    assert e.value.path == groups
    assert groups.read_text(encoding="utf-8") == text


# ---- readable start-up failure on the command line ----

def run_module(*argv: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DISCORD_", "QQ_BOT_"))}
    env["PYTHONIOENCODING"] = "utf-8"  # as on the server (Linux, UTF-8); a Windows pipe defaults to the ANSI code page
    return subprocess.run([sys.executable, "-m", *argv], cwd=REPO, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


def assert_readable(output: str, path) -> None:
    assert f"{path} 已损坏" in output and "JSONDecodeError" in output and "备份恢复" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("module", [["backend.monitor.server"], ["backend.monitor", "--once"]])
def test_the_dashboard_and_the_monitor_refuse_to_start_with_one_readable_message(tmp_path, module):
    roster = damage(tmp_path / "data" / "streamers.json")

    r = run_module(*module, "--config", str(write_config(tmp_path)))
    assert r.returncode != 0
    assert_readable(r.stderr, roster)


@pytest.mark.parametrize("argv", [["list"], ["add-user", "newbie", "--password-stdin"], ["disable", "boss"]])
def test_the_user_command_line_reports_a_damaged_users_file_the_same_way(tmp_path, monkeypatch, capsys, argv):
    users = damage(tmp_path / "data" / "users.json")
    monkeypatch.setattr("sys.stdin", io.StringIO("secret-pass\n"))

    assert auth_cli.main(["--config", str(write_config(tmp_path, auth=True)), *argv]) == 1
    out = capsys.readouterr()
    assert_readable(out.err, users)
    assert users.read_text(encoding="utf-8") == DAMAGED


# ---- while the server runs ----

ORIGIN = {"Origin": "http://testserver"}


def running_app(tmp_path, monkeypatch, sessions: str | None = None):
    """An app with login on, users boss (admin) and fan, and a recording alert channel;
    `sessions`: the sessions file's text when the app starts."""
    monkeypatch.setitem(Notifier.registry, "rec", AlertRecorder)
    monkeypatch.setenv("KIRI_T_REC", "https://alerts.test/hook")
    cfg = write_config(tmp_path, auth=True)
    body = json.loads(cfg.read_text(encoding="utf-8"))
    body["notify"]["rec"] = {"webhook_env": "KIRI_T_REC"}
    cfg.write_text(json.dumps(body), encoding="utf-8")
    for name, role in (("boss", ["--admin"]), ("fan", [])):
        monkeypatch.setattr("sys.stdin", io.StringIO("secret-pass\n"))
        assert auth_cli.main(["--config", str(cfg), "add-user", name, *role, "--password-stdin"]) == 0
    if sessions is not None:
        damage(tmp_path / "data" / "sessions.json", sessions)
    app = create_app(cfg)
    app.state.auth.fail_delay = 0
    return app, app.state.notifier.notifiers[0]


def login(client, name: str) -> int:
    return client.post("/api/login", json={"username": name, "password": "secret-pass"}).status_code


def names(client) -> list[str]:
    return [u["username"] for u in client.get("/api/users").json()["users"]]


def wait_for(condition, timeout: float = 5) -> bool:
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True


def test_users_damaged_while_running_stay_in_effect_and_the_owner_is_alerted(tmp_path, monkeypatch):
    app, rec = running_app(tmp_path, monkeypatch)
    users = tmp_path / "data" / "users.json"

    with TestClient(app, headers=ORIGIN) as client:
        assert login(client, "boss") == 200
        good = users.read_text(encoding="utf-8")
        damage(users)

        assert names(client) == ["boss", "fan"]  # never an empty user list
        assert client.get("/api/me").json()["username"] == "boss"
        assert login(client, "fan") == 200 and client.get("/api/me").json()["username"] == "fan"
        assert login(client, "boss") == 200
        assert wait_for(lambda: rec.alerts)
        assert rec.alerts == ["users.json 损坏，继续使用损坏前读到的内容，文件未改动，请修复或从备份恢复"]

        # account changes are refused: the damaged file is left for a person to repair
        r = client.post("/api/users", json={"username": "newbie", "password": "longenough"})
        assert r.status_code == 503 and "users.json" in r.json()["detail"]
        assert client.patch("/api/users", params={"name": "fan"}, json={"disabled": True}).status_code == 503
        assert client.delete("/api/users", params={"name": "fan"}).status_code == 503
        assert users.read_text(encoding="utf-8") == DAMAGED
        assert names(client) == ["boss", "fan"]

        # repaired (here: restored with one account fewer): read again, no second alert
        restored = [u for u in json.loads(good) if u["username"] == "boss"]
        users.write_text(json.dumps(restored), encoding="utf-8")
        assert names(client) == ["boss"]
        assert client.post("/api/users", json={"username": "newbie", "password": "longenough"}).status_code == 201
        time.sleep(0.2)
        assert len(rec.alerts) == 1


# ---- the sessions: runtime state ----

def session_backups(tmp_path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "data").iterdir() if p.name.startswith("sessions.json.corrupt-"))


def test_a_damaged_sessions_file_is_backed_up_reported_and_everyone_logs_in_again(tmp_path, monkeypatch):
    app, rec = running_app(tmp_path, monkeypatch, sessions=DAMAGED)
    sessions = tmp_path / "data" / "sessions.json"

    with TestClient(app, headers=ORIGIN) as client:
        (backup,) = session_backups(tmp_path)
        assert (tmp_path / "data" / backup).read_text(encoding="utf-8") == DAMAGED
        assert wait_for(lambda: rec.alerts)
        assert rec.alerts == [f"sessions.json 损坏，已备份为 {backup}，已按空文件继续运行"]
        assert client.get("/api/me").status_code == 401
        assert login(client, "fan") == 200
        assert client.get("/api/me").json()["username"] == "fan"
        assert sessions.exists()


def test_sessions_damaged_while_running_are_set_aside_and_re_read_from_disk_afterwards(tmp_path, monkeypatch):
    app, rec = running_app(tmp_path, monkeypatch)
    sessions = tmp_path / "data" / "sessions.json"

    with TestClient(app, headers=ORIGIN) as client:
        assert login(client, "fan") == 200
        damage(sessions, '{"abc": {"username": "fan"}}')  # valid JSON, not sessions

        assert client.get("/api/me").status_code == 401
        assert len(session_backups(tmp_path)) == 1
        assert wait_for(lambda: len(rec.alerts) == 1)
        assert login(client, "fan") == 200
        assert client.get("/api/me").status_code == 200

        sessions.write_text("{}", encoding="utf-8")  # another process (the CLI) ends every session
        assert client.get("/api/me").status_code == 401
        assert len(rec.alerts) == 1
