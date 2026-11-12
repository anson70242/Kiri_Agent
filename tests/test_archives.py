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
    (done / f"{STEM}.transcript.json").write_text('[{"start": 1.0, "end": 2.0, "text": "やっほー"}]', encoding="utf-8")
    table = {"streamer": "柊優花", "title": "歌枠", "rows": [
        {"start": "00:00:00", "end": "00:05:00", "score": 8, "top_pick": {"title": "开场"}},
        {"start": "00:05:00", "end": "00:10:00", "score": 5, "top_pick": None},
    ]}
    (done / f"{STEM}.highlights.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
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
    assert done["transcript_file"] == f"{STEM}.transcript.json"
    assert newest["transcript_file"] is None


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


def test_subtitles_vtt(client):
    r = client.get("/api/archives/subtitles.vtt", params={"folder": FOLDER})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/vtt")
    assert r.text == "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nやっほー\n"
    r = client.get("/api/archives/subtitles.vtt", params={"folder": "柊優花/20261106/雑談"})
    assert r.status_code == 404  # no transcript yet
    assert client.get("/api/archives/subtitles.vtt", params={"folder": "../.."}).status_code == 404


def test_transcript(client):
    r = client.get("/api/archives/transcript", params={"folder": FOLDER})
    assert r.json() == {"segments": [{"start": 1.0, "end": 2.0, "text": "やっほー"}]}
    assert client.get("/api/archives/transcript", params={"folder": "柊優花/20261106/雑談"}).status_code == 404


def test_highlights_list_and_table(client):
    listing = client.get("/api/highlights").json()["tables"]
    assert [(t["folder"], t["creator"], t["date"], t["title"]) for t in listing] == [(FOLDER, "柊優花", "20261105", "歌枠")]
    assert (listing[0]["segments"], listing[0]["best_score"], listing[0]["picks"]) == (2, 8, 1)

    table = client.get("/api/highlights/table", params={"folder": FOLDER}).json()
    assert [r["start"] for r in table["rows"]] == ["00:00:00", "00:05:00"]
    assert client.get("/api/highlights/table", params={"folder": "柊優花/20261106/雑談"}).status_code == 404
    assert client.get("/api/highlights/table", params={"folder": "../.."}).status_code == 404


def test_highlights_page(client):
    r = client.get("/highlights")
    assert r.status_code == 200 and "路灯表" in r.text


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
