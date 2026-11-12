"""GET /api/downloads: auto-download progress and queue for the archives page."""

import json

from fastapi.testclient import TestClient

from backend.downloader.auto import Job
from backend.monitor.server import create_app

from .helpers import live


def write_config(tmp_path, auto: bool):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a", "auto_download": auto}]},
        "notify": {"discord": {"enabled": False}},
        "download": {
            "output_dir": str(tmp_path / "dl"),
            "auto": {"state_file": str(tmp_path / "queue.json")},
        },
    }), encoding="utf-8")
    return cfg


def test_downloads_lists_queue(tmp_path):
    cfg = write_config(tmp_path, auto=True)
    job = Job(live(), queued_at=100.0, next_try=200.0, attempts=1, last_error="stream is post_live")
    (tmp_path / "queue.json").write_text(json.dumps([job.to_dict()]), encoding="utf-8")

    body = TestClient(create_app(cfg)).get("/api/downloads").json()

    assert body["enabled"] is True and body["active"] == []
    (row,) = body["queue"]
    assert (row["name"], row["title"], row["attempts"], row["next_try"]) == ("A", "title v1", 1, 200.0)


def test_dev_mode_never_downloads_or_notifies(tmp_path):
    cfg = write_config(tmp_path, auto=True)  # auto_download on in the config...
    app = create_app(cfg, dev=True)  # ...but a local preview must not act on it
    assert TestClient(app).get("/api/downloads").json() == {"enabled": False, "active": [], "queue": []}
    assert app.state.notifier is None


def test_downloads_when_auto_download_is_off(tmp_path):
    cfg = write_config(tmp_path, auto=False)
    body = TestClient(create_app(cfg)).get("/api/downloads").json()
    assert body == {"enabled": False, "active": [], "queue": []}
