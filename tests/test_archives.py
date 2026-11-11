"""Archives router: listing, chat, file serving, and path traversal."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.monitor.archives import create_router

FOLDER = "柊優花/20261105/歌枠"
STEM = "[20261105][柊優花] 歌枠"


@pytest.fixture
def client(tmp_path):
    out = tmp_path / "downloads"
    done = out / FOLDER
    done.mkdir(parents=True)
    (done / f"{STEM}.mp4").write_bytes(b"0123456789")
    chat = [
        {"time": "00:00:05", "user": "a", "msg": "hi"},
        {"time": "00:00:30", "user": "b", "msg": "yo"},
        {"time": "00:02:10", "user": "c", "msg": "8888", "sc": "¥500"},
    ]
    (done / f"{STEM}.chat_parsed.json").write_text(json.dumps(chat), encoding="utf-8")
    partial = out / "柊優花/20261106/雑談"
    partial.mkdir(parents=True)
    (partial / "[20261106][柊優花] 雑談.mp4.part").write_bytes(b"xx")
    (tmp_path / "config.json").write_text("{}")  # traversal target

    app = FastAPI()
    app.include_router(create_router(out))
    return TestClient(app)


def test_list(client):
    data = client.get("/api/archives").json()
    assert data["disk"]["total"] > 0
    newest, done = data["archives"]
    assert newest["date"] == "20261106" and newest["downloading"] and newest["video"] is None
    assert done["folder"] == FOLDER
    assert done["creator"] == "柊優花" and done["title"] == "歌枠"
    assert done["video"] == f"{STEM}.mp4" and done["video_size"] == 10
    assert not done["downloading"]
    assert done["has_chat"] and done["chat_count"] == 3


def test_list_missing_dir(tmp_path):
    app = FastAPI()
    app.include_router(create_router(tmp_path / "nope"))
    assert TestClient(app).get("/api/archives").json()["archives"] == []


def test_chat(client):
    data = client.get("/api/archives/chat", params={"folder": FOLDER}).json()
    assert len(data["messages"]) == 3
    assert data["density"] == [2, 0, 1]
    r = client.get("/api/archives/chat", params={"folder": "柊優花/20261106/雑談"})
    assert r.status_code == 404


def test_file_and_range(client):
    params = {"folder": FOLDER, "name": f"{STEM}.mp4"}
    r = client.get("/api/archives/file", params=params)
    assert r.status_code == 200 and r.content == b"0123456789"
    r = client.get("/api/archives/file", params=params, headers={"Range": "bytes=2-4"})
    assert r.status_code == 206 and r.content == b"234"


@pytest.mark.parametrize(
    "params",
    [
        {"folder": "../", "name": "config.json"},
        {"folder": "..", "name": "config.json"},
        {"folder": FOLDER, "name": "../../../../config.json"},
        {"folder": FOLDER, "name": "..\\..\\..\\..\\config.json"},
        {"folder": "", "name": "x"},
        {"folder": FOLDER, "name": "missing.mp4"},
    ],
)
def test_file_rejects_traversal(client, params):
    r = client.get("/api/archives/file", params=params)
    assert r.status_code in (400, 404)


def test_chat_rejects_traversal(client):
    assert client.get("/api/archives/chat", params={"folder": "../.."}).status_code == 404


def test_page(client):
    r = client.get("/archives")
    assert r.status_code == 200 and "录播" in r.text
