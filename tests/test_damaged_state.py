"""Start-up with a damaged runtime state file: the app runs on, the file is kept as a
backup, and the owner gets the operator alert."""

from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient

from backend.monitor.server import create_app
from backend.notify import Notifier
from backend.state_file import StateFile

from .helpers import AlertRecorder, wait_for

DAMAGED = "[{\"status\": "


def write_config(tmp_path, alerts: bool = True):
    notify = {"state_file": str(tmp_path / "notify_state.json"), "log_file": str(tmp_path / "notify_log.json")}
    notify["rec" if alerts else "discord"] = {"webhook_env": "KIRI_T_REC"} if alerts else {"enabled": False}
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": []},  # nobody to poll: no network
        "notify": notify,
        "download": {
            "output_dir": str(tmp_path / "dl"),
            "chat_retry": {"state_file": str(tmp_path / "pending.json")},
            "auto": {"state_file": str(tmp_path / "queue.json")},
        },
    }), encoding="utf-8")
    return cfg


def setup(tmp_path, monkeypatch, alerts: bool = True):
    monkeypatch.setitem(Notifier.registry, "rec", AlertRecorder)
    monkeypatch.setenv("KIRI_T_REC", "https://alerts.test/hook")
    (tmp_path / "queue.json").write_text(DAMAGED, encoding="utf-8")
    return create_app(write_config(tmp_path, alerts))


def backups(tmp_path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("queue.json.corrupt-"))


def test_a_damaged_download_queue_is_backed_up_and_reported_and_the_app_runs_on(tmp_path, monkeypatch):
    app = setup(tmp_path, monkeypatch)
    rec = app.state.notifier.notifiers[0]

    with TestClient(app) as client:
        assert client.get("/api/downloads").json()["queue"] == []
        assert wait_for(lambda: rec.alerts)
        (backup,) = backups(tmp_path)
        assert rec.alerts == [f"queue.json 损坏，已备份为 {backup}，已按空文件继续运行"]
        assert (tmp_path / backup).read_text(encoding="utf-8") == DAMAGED
        assert client.get("/healthz").json() == {"ok": True}


def test_an_incident_before_the_notifier_runs_is_held_until_it_does(tmp_path, monkeypatch):
    app = setup(tmp_path, monkeypatch)
    rec = app.state.notifier.notifiers[0]

    assert app.state.auto.snapshot()["queue"] == []  # read outside any event loop, before start
    assert backups(tmp_path) and rec.alerts == []

    with TestClient(app):
        assert wait_for(lambda: len(rec.alerts) == 1)


def test_an_incident_in_a_worker_thread_reaches_the_alert(tmp_path, monkeypatch):
    app = setup(tmp_path, monkeypatch)
    rec = app.state.notifier.notifiers[0]

    with TestClient(app):
        assert wait_for(lambda: len(rec.alerts) == 1)
        (tmp_path / "queue.json").write_text("{damaged again", encoding="utf-8")
        worker = threading.Thread(target=app.state.auto.load)
        worker.start()
        worker.join()
        assert wait_for(lambda: len(rec.alerts) == 2)


def test_with_no_alert_channel_the_incident_is_only_logged(tmp_path, monkeypatch, caplog):
    app = setup(tmp_path, monkeypatch, alerts=False)

    with TestClient(app) as client:
        assert client.get("/api/downloads").json()["queue"] == []
    assert backups(tmp_path) and "queue.json" in caplog.text


def test_dev_mode_only_logs_incidents(tmp_path, monkeypatch, caplog):
    app = setup(tmp_path, monkeypatch)
    rec = app.state.notifier.notifiers[0]
    with TestClient(app):
        assert wait_for(lambda: len(rec.alerts) == 1)
        create_app(write_config(tmp_path), dev=True)  # a local preview takes over this process

        (tmp_path / "x.json").write_text("{oops", encoding="utf-8")
        assert StateFile(tmp_path / "x.json", default=list, policy="recover").load() == []
        time.sleep(0.2)
    assert len(rec.alerts) == 1 and "x.json" in caplog.text
