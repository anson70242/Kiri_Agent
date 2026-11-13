"""Archives router: listing, chat, file serving, and path traversal."""

import io
import zipfile

import openpyxl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.monitor.archives import create_router

from .helpers import sample_archive

FOLDER = "柊優花/20261105/歌枠"
STEM = "[20261105][柊優花] 歌枠"


@pytest.fixture
def client(tmp_path):
    out = tmp_path / "downloads"
    table = {"streamer": "柊優花", "title": "歌枠", "rows": [
        {"start": "00:00:00", "end": "00:05:00", "score": 8, "top_pick": {"title": "开场"}, "timeline": [
            {"start": "00:00", "end": "02:00", "title": "开场", "detail": "打招呼"},
            {"start": "02:00", "end": "05:00", "title": "杂谈", "detail": "聊天"}]},
        {"start": "00:05:00", "end": "00:10:00", "score": 5, "top_pick": None},
    ]}
    # every artifact (helpers.CHAT has 3 messages); the raw chat is not in the zip
    sample_archive(out, *FOLDER.split("/"), highlights=table, raw_chat={})
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


def test_a_highlight_table_that_is_not_an_object_counts_as_damaged(client, tmp_path):
    sample_archive(tmp_path / "downloads", "柊優花", "20261107", "朝活", highlights=[{"start": "00:00:00"}])

    listing = client.get("/api/highlights")
    assert listing.status_code == 200 and [t["folder"] for t in listing.json()["tables"]] == [FOLDER]  # skipped
    r = client.get("/api/highlights/table", params={"folder": "柊優花/20261107/朝活"})
    assert (r.status_code, r.json()["detail"]) == (500, "路灯表 is not valid JSON")
    assert client.get("/api/highlights/xlsx", params={"folder": "柊優花/20261107/朝活"}).status_code == 500


def test_listing_says_which_archives_have_a_table(client):
    newest, done = client.get("/api/archives").json()["archives"]
    assert done["highlights_file"] == f"{STEM}.highlights.json"
    assert newest["highlights_file"] is None


def test_highlights_xlsx(client):
    r = client.get("/api/highlights/xlsx", params={"folder": FOLDER})
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
    rows = [[c.value for c in row] for row in ws.iter_rows()]
    assert rows[0] == ["开始", "结束", "话题分类", "内容摘要（话题日志）", "切片标题 / 高能点", "分数", "弹幕", "剪辑"]
    assert rows[1][:2] == ["00:00:00", "00:05:00"]
    assert rows[1][3] == "00:00–02:00 开场 打招呼\n\n02:00–05:00 杂谈 聊天"  # blank line between entries
    assert rows[1][4] == "🏆 开场" and rows[1][5] == 8  # a number, so it sorts and filters
    # Readable as opened: header stays put, long cells wrap, picks stand out.
    assert ws.freeze_panes == "A2"
    assert ws["D2"].alignment.wrap_text
    # The long-text columns are the wide ones; 内容摘要 the widest (~50 CJK characters a line).
    widths = {c: ws.column_dimensions[c].width for c in "ABCDEFGH"}
    assert widths["D"] >= 100 and 60 <= widths["E"] < widths["D"]
    assert max(w for c, w in widths.items() if c not in "DE") < widths["E"]
    assert ws["A2"].fill.fgColor.rgb.endswith("FFFBEB")  # row with a pick
    assert ws["A3"].fill.fill_type is None  # row without
    # Every cell vertically centred. Horizontally: headers A-G centred; data A-G centred
    # except the long text (内容摘要 D, 切片标题 E), which reads better left-aligned; 剪辑 (H) as is.
    for row in ws.iter_rows(min_row=1, max_row=3):
        for c in row:
            assert c.alignment.vertical == "center", c.coordinate
            want = None if c.column_letter == "H" else \
                "left" if c.row > 1 and c.column_letter in "DE" else "center"
            assert c.alignment.horizontal == want, c.coordinate
            if c.row > 1:
                assert c.alignment.wrap_text, c.coordinate  # multi-line cells keep wrapping
    assert client.get("/api/highlights/xlsx", params={"folder": "柊優花/20261106/雑談"}).status_code == 404


def test_zip_has_what_editors_use(client):
    # The video, the cleaned chat, and subtitles as SRT in both languages; nothing else.
    r = client.get("/api/archives/zip", params={"folder": FOLDER})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == sorted([
        f"{STEM}.mp4", f"{STEM}.chat_parsed.json", f"{STEM}.zh.srt", f"{STEM}.ja.srt",
    ])
    assert z.read(f"{STEM}.mp4") == b"0123456789"
    assert "やっほー" in z.read(f"{STEM}.ja.srt").decode() and "哈喽" in z.read(f"{STEM}.zh.srt").decode()
    assert client.get("/api/archives/zip", params={"folder": "../.."}).status_code == 404


def test_chinese_subtitles(client):
    newest, done = client.get("/api/archives").json()["archives"]
    assert done["transcript_file"] == f"{STEM}.transcript.json"  # not the .zh one
    assert done["zh_transcript_file"] == f"{STEM}.zh.transcript.json"
    assert newest["zh_transcript_file"] is None

    q = {"folder": FOLDER}
    assert client.get("/api/archives/transcript", params=q).json()["segments"][0]["text"] == "やっほー"
    assert client.get("/api/archives/transcript", params=q | {"lang": "zh"}).json()["segments"][0]["text"] == "哈喽"
    assert "哈喽" in client.get("/api/archives/subtitles.vtt", params=q | {"lang": "zh"}).text

    r = client.get("/api/archives/srt", params=q | {"lang": "zh"})
    assert "attachment" in r.headers["content-disposition"]
    assert r.text == "1\n00:00:01,000 --> 00:00:02,000\n哈喽\n\n"
    assert client.get("/api/archives/srt", params={"folder": "柊優花/20261106/雑談", "lang": "zh"}).status_code == 404


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
