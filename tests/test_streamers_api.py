"""/api/streamers: the streamer list, edited from the dashboard and kept in data/.

config.json is overwritten on every deploy, so the dashboard's edits live in a
separate file (`monitor.streamers_file`) that config.json only seeds.
"""

import json

from fastapi.testclient import TestClient

from backend.monitor.server import create_app
from backend.notify import EventKind, StreamEvent

from .helpers import B, live

SEED = [
    {"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True},
    {"name": "B", "platform": "twitch", "channel": "b"},
]


def write_config(tmp_path, streamers=SEED):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": streamers, "streamers_file": str(tmp_path / "data" / "streamers.json")},
        "notify": {"discord": {"enabled": False}},
        "download": {
            "output_dir": str(tmp_path / "dl"),
            "auto": {"state_file": str(tmp_path / "queue.json")},
        },
    }), encoding="utf-8")
    return cfg


def test_seeded_from_config(tmp_path):
    body = TestClient(create_app(write_config(tmp_path))).get("/api/streamers").json()
    assert [(s["id"], s["name"], s["auto_download"]) for s in body["streamers"]] == [
        ("youtube:@a", "A", True),
        ("twitch:b", "B", False),
    ]


async def channel_exists(streamer):
    """Stand-in for the real platform check: only "@missing" doesn't exist."""
    return "channel not found" if streamer.channel == "@missing" else None


def app_for(cfg):
    return TestClient(create_app(cfg, check_channel=channel_exists))


def names(client):
    return [s["name"] for s in client.get("/api/status").json()["streamers"]]


def test_add_is_monitored_and_kept(tmp_path):
    cfg = write_config(tmp_path)
    client = app_for(cfg)

    r = client.post("/api/streamers", json={"name": "C", "platform": "youtube", "channel": "@c"})

    assert r.status_code == 201
    assert names(client) == ["A", "B", "C"]
    assert names(app_for(cfg)) == ["A", "B", "C"]  # after a restart (and a deploy: config.json unchanged)


def test_add_rejects_bad_input(tmp_path):
    client = app_for(write_config(tmp_path))
    post = lambda **body: client.post("/api/streamers", json=body).status_code  # noqa: E731

    assert post(name="A2", platform="youtube", channel="@a") == 409  # already there
    assert post(name="X", platform="niconico", channel="x") == 400
    assert post(name="X", platform="youtube", channel="@missing") == 422
    assert post(name="X", platform="youtube", channel="  ") == 400
    assert names(client) == ["A", "B"]


def queued(client):
    return [row["name"] for row in client.get("/api/downloads").json()["queue"]]


def test_gear_turns_auto_download_on(tmp_path):
    cfg = write_config(tmp_path, streamers=[{"name": "B", "platform": "twitch", "channel": "b"}])
    client = app_for(cfg)
    ended = StreamEvent(EventKind.OFFLINE, live(B))
    client.app.state.auto.on_ended(ended)
    assert queued(client) == []  # off: an ended stream isn't downloaded

    r = client.patch("/api/streamers", params={"id": "twitch:b"}, json={"auto_download": True, "keep": 3})

    assert r.status_code == 200
    client.app.state.auto.on_ended(ended)
    assert queued(client) == ["B"]
    (b,) = app_for(cfg).get("/api/streamers").json()["streamers"]  # kept across a restart
    assert (b["auto_download"], b["keep"]) == (True, 3)


def test_gear_rejects_unknown_streamer_and_settings(tmp_path):
    client = app_for(write_config(tmp_path))
    assert client.patch("/api/streamers", params={"id": "twitch:zzz"}, json={"auto_download": True}).status_code == 404
    assert client.patch("/api/streamers", params={"id": "twitch:b"}, json={"keep": 0}).status_code == 422


def test_delete_stops_monitoring_but_keeps_archives(tmp_path):
    cfg = write_config(tmp_path)
    archive = tmp_path / "dl" / "A" / "20261110" / "歌枠" / "v.mp4"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"x")
    client = app_for(cfg)

    assert client.delete("/api/streamers", params={"id": "youtube:@a"}).status_code == 200

    assert names(client) == ["B"]
    assert names(app_for(cfg)) == ["B"]
    assert archive.exists()
    assert client.delete("/api/streamers", params={"id": "youtube:@a"}).status_code == 404
