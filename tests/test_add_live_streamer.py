"""A streamer added from the dashboard while live: announced like any live, and so is its end."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.monitor import LiveState
from backend.monitor.server import create_app
from backend.notify import EventKind, Notifier, StreamEvent

from .helpers import wait_for
from .test_monitor import SCRIPT  # importing it registers the scripted platform "fake"


class Sent(Notifier):
    """A channel that records what it is sent, built the way the config builds channels."""

    name = ""  # empty: registered only by the test

    def __init__(self, url: str, mention: str | None = None):
        self.events: list[tuple[EventKind, str, str | None]] = []

    async def send(self, event: StreamEvent) -> None:
        self.events.append((event.kind, event.status.streamer.name, event.status.stream_id))


def running_app(tmp_path, monkeypatch):
    monkeypatch.setitem(Notifier.registry, "sent", Sent)
    monkeypatch.setenv("KIRI_T_SENT", "https://sent.test/hook")
    # Not a first start: the service already knows somebody, so a new live is news.
    (tmp_path / "notify_state.json").write_text(
        json.dumps({"version": 1, "streamers": {"youtube:@somebody": None}}), encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [], "interval": 3600, "streamers_file": str(tmp_path / "streamers.json")},
        "notify": {"state_file": str(tmp_path / "notify_state.json"), "log_file": str(tmp_path / "notify_log.json"),
                   "offline_grace": 0.05, "sent": {"webhook_env": "KIRI_T_SENT"}},
        "download": {"output_dir": str(tmp_path / "dl"),
                     "chat_retry": {"state_file": str(tmp_path / "pending.json")},
                     "auto": {"state_file": str(tmp_path / "queue.json")}},
    }), encoding="utf-8")
    app = create_app(cfg)
    return app, app.state.notifier.notifiers[0]


def test_a_streamer_added_while_live_is_announced_and_so_is_the_end(tmp_path, monkeypatch):
    app, sent = running_app(tmp_path, monkeypatch)
    # What the platform answers: live when she is added and at the next check, then over.
    SCRIPT["hanon"] = [("live", "v1"), ("live", "v1"), LiveState.OFFLINE, LiveState.OFFLINE]

    with TestClient(app) as client:
        added = client.post("/api/streamers", json={"name": "Hanon", "platform": "fake", "channel": "hanon"})
        assert added.status_code == 201
        assert wait_for(lambda: sent.events == [(EventKind.LIVE, "Hanon", "v1")]), sent.events

        client.post("/api/refresh")  # still live: nothing new
        client.post("/api/refresh")  # over
        assert wait_for(lambda: len(sent.events) == 2), sent.events
        assert sent.events[1] == (EventKind.OFFLINE, "Hanon", "v1")


def test_a_streamer_added_while_offline_is_announced_when_she_goes_live(tmp_path, monkeypatch):
    app, sent = running_app(tmp_path, monkeypatch)
    SCRIPT["kotoha"] = [LiveState.OFFLINE, LiveState.OFFLINE, ("live", "v7")]

    with TestClient(app) as client:
        assert client.post("/api/streamers", json={"name": "Kotoha", "platform": "fake", "channel": "kotoha"}).status_code == 201
        client.post("/api/refresh")
        assert sent.events == []
        client.post("/api/refresh")
        assert wait_for(lambda: sent.events == [(EventKind.LIVE, "Kotoha", "v7")]), sent.events
