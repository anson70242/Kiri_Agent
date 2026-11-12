"""Uploading a 路灯表 to Tencent Docs, against a fake docs.qq.com (httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from backend.sheet import HEADER
from backend.tencent_docs import TencentDocsError, TencentSettings, upload_table

FILE_ID = "FAKEFILE"
SECRETS = {"access_token": "tok-SECRET-123", "client_id": "cid-SECRET", "open_id": "oid-SECRET"}


def settings(**kw) -> TencentSettings:
    return TencentSettings(**{**SECRETS, "file_id": FILE_ID, "retry_delay": 0, **kw})


def row(i: int, **kw) -> dict:
    return {"start": f"00:{i:02d}:00", "end": f"00:{i + 5:02d}:00", "topics": ["杂谈", "游戏"],
            "timeline": [{"start": "00:00:00", "end": "00:02:00", "title": "【开场】", "detail": "打招呼"},
                         {"start": "00:02:00", "end": "00:05:00", "title": "【猫】", "detail": "猫来了"}],
            "highlights": [], "score": 5, "chat_level": "中", **kw}


class FakeDocs:
    """Records every request; answers like the v3 spreadsheet API (HTTP 200, status in `code`)."""

    def __init__(self, sheets=("工作表1",), errors=None):
        self.sheets = [{"sheetId": f"s{i}", "title": t} for i, t in enumerate(sheets)]
        self.requests: list[httpx.Request] = []
        self.errors = list(errors or [])  # (predicate(kind), body) answered once each

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        kind = self.kind(request)
        for i, (match, body) in enumerate(self.errors):
            if match(kind):
                del self.errors[i]
                return httpx.Response(200, json=body)
        if request.method == "GET":
            return httpx.Response(200, json={"properties": self.sheets})
        out = []
        for req in json.loads(request.content)["requests"]:
            if add := req.get("addSheetRequest"):
                props = {"sheetId": "NEW1", "title": add["title"], "rowCount": add["rowCount"],
                         "columnCount": add["columnCount"]}
                self.sheets.append(props)
                out.append({"addSheetResponse": {"properties": props}})
            elif "deleteSheetRequest" in req:
                sid = req["deleteSheetRequest"]["sheetId"]
                self.sheets = [s for s in self.sheets if s["sheetId"] != sid]
                out.append({})
            else:
                out.append({})
        return httpx.Response(200, json={"code": 0, "message": "", "responses": out})

    @staticmethod
    def kind(request: httpx.Request) -> str:
        if request.method == "GET":
            return "list"
        return next(iter(json.loads(request.content)["requests"][0]))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def bodies(self, kind: str) -> list[dict]:
        return [json.loads(r.content)["requests"][0][kind] for r in self.requests if self.kind(r) == kind]

    def written(self) -> list[list]:
        """All updateRangeRequest cells, as plain values, placed at their start rows."""
        grid: dict[int, list] = {}
        for body in self.bodies("updateRangeRequest"):
            g = body["gridData"]
            for i, r in enumerate(g["rows"]):
                grid[g["startRow"] + i] = [next(iter(c["cellValue"].values())) for c in r["values"]]
        return [grid[i] for i in sorted(grid)]


def test_upload_creates_a_sheet_and_writes_header_and_rows():
    fake = FakeDocs()
    table = {"streamer": "鹿乃まほろ", "title": "T", "rows": [row(0, top_pick={"start": "00:01:00", "end": "00:02:00",
                                                                             "title": "名场面"}), row(5)]}
    url = upload_table(table, settings(), sheet_title="20261111 鹿乃まほろ T", client=fake.client())

    assert url == f"https://docs.qq.com/sheet/{FILE_ID}?tab=NEW1"
    kinds = [fake.kind(r) for r in fake.requests]
    assert kinds[:2] == ["list", "addSheetRequest"] and set(kinds[2:]) == {"updateRangeRequest"}
    assert fake.bodies("addSheetRequest")[0]["title"] == "20261111 鹿乃まほろ T"
    assert all(b["sheetId"] == "NEW1" for b in fake.bodies("updateRangeRequest"))

    grid = fake.written()
    assert grid[0] == HEADER
    assert len(grid) == 3
    assert grid[1][0] == "00:00:00" and grid[1][2] == "杂谈\n游戏"
    assert grid[1][3] == "00:00:00–00:02:00 【开场】 打招呼\n\n00:02:00–00:05:00 【猫】 猫来了"
    assert grid[1][4].startswith("🏆 00:01:00–00:02:00 名场面")
    assert grid[1][5] == 5  # the score goes up as a number, not text
    for r in fake.requests:
        assert r.headers["Access-Token"] == SECRETS["access_token"]
        assert r.headers["Client-Id"] == SECRETS["client_id"]
        assert r.headers["Open-Id"] == SECRETS["open_id"]
    assert fake.requests[0].url.path == f"/openapi/spreadsheet/v3/files/{FILE_ID}"
    assert fake.requests[1].url.path == f"/openapi/spreadsheet/v3/files/{FILE_ID}/batchUpdate"


def test_a_long_table_is_written_in_batches_and_the_sheet_fits_the_cell_limit():
    from backend.tencent_docs import MAX_CELLS, ROWS_PER_BATCH

    fake = FakeDocs()
    n = ROWS_PER_BATCH * 2 + 7  # 10 h of 5-minute segments would be 120
    table = {"rows": [row(i % 50, score=i) for i in range(n)]}
    upload_table(table, settings(), sheet_title="t", client=fake.client())

    writes = fake.bodies("updateRangeRequest")
    assert len(writes) == 3
    assert [w["gridData"]["startRow"] for w in writes] == [0, ROWS_PER_BATCH, ROWS_PER_BATCH * 2]
    assert all(len(w["gridData"]["rows"]) <= ROWS_PER_BATCH for w in writes)
    grid = fake.written()
    assert len(grid) == n + 1 and grid[0] == HEADER and [g[5] for g in grid[1:]] == list(range(n))
    add = fake.bodies("addSheetRequest")[0]
    assert add["rowCount"] >= n + 1 and add["columnCount"] >= len(HEADER)
    assert add["rowCount"] * add["columnCount"] <= MAX_CELLS


def test_a_table_too_big_for_one_sheet_is_refused_before_any_request():
    fake = FakeDocs()
    with pytest.raises(TencentDocsError, match="10000"):
        upload_table({"rows": [row(0)] * 1300}, settings(), sheet_title="t", client=fake.client())
    assert fake.requests == []


def test_uploading_again_replaces_the_old_sheet():
    fake = FakeDocs(sheets=["工作表1", "20261111 A T"])
    upload_table({"rows": [row(0)]}, settings(), sheet_title="20261111 A T", client=fake.client())
    assert [fake.kind(r) for r in fake.requests][:3] == ["list", "deleteSheetRequest", "addSheetRequest"]
    assert fake.bodies("deleteSheetRequest") == [{"sheetId": "s1"}]
    assert [s["title"] for s in fake.sheets] == ["工作表1", "20261111 A T"]


def test_the_only_sheet_is_never_deleted():
    # A spreadsheet can't be left with no sheet: keep it and number the new one.
    fake = FakeDocs(sheets=["20261111 A T"])
    upload_table({"rows": [row(0)]}, settings(), sheet_title="20261111 A T", client=fake.client())
    assert fake.bodies("deleteSheetRequest") == []
    assert fake.bodies("addSheetRequest")[0]["title"] == "20261111 A T (2)"


@pytest.mark.parametrize("text, file_id", [
    ("https://docs.qq.com/sheet/DUmFabcXYZ123?tab=BB08J2", "DUmFabcXYZ123"),
    ("docs.qq.com/sheet/DUmFabcXYZ123", "DUmFabcXYZ123"),
    ("  DUmFabcXYZ123 ", "DUmFabcXYZ123"),
    ("https://docs.qq.com/doc/DUmFabcXYZ123", None),  # a document, not a spreadsheet
    ("https://example.com/sheet/x", None),
    ("not an id!", None),
    ("", None),
])
def test_file_id_from_a_link_or_an_id(text, file_id):
    from backend.tencent_docs import parse_file_id

    assert parse_file_id(text) == file_id


def test_sheet_title_is_date_creator_title_trimmed_and_safe():
    from backend.tencent_docs import MAX_TITLE, sheet_title, unique_title

    t = sheet_title({"streamer": "鹿乃まほろ", "title": "【Potato Please】60,000個のじゃがいもの皮って何時間で剥けますか？【鹿乃まほろ/ミリプロ】"},
                    "20261111")
    assert t.startswith("20261111 鹿乃まほろ 【Potato Please】") and len(t) <= MAX_TITLE
    assert "/" not in sheet_title({"title": "a/b[c]:d"})
    long = "x" * MAX_TITLE
    assert unique_title(long, {long}) == "x" * (MAX_TITLE - 4) + " (2)"


def test_an_expired_token_is_a_clear_error_and_never_shows_the_token():
    expired = {"code": 400006, "message": "Authentication Internal Error"}
    fake = FakeDocs(errors=[(lambda k: k == "list", expired)])
    with pytest.raises(TencentDocsError) as e:
        upload_table({"rows": [row(0)]}, settings(), sheet_title="t", client=fake.client())
    assert e.value.code == 400006
    assert "400006" in str(e.value) and "token" in str(e.value).lower()
    for secret in SECRETS.values():
        assert secret not in str(e.value) and secret not in repr(settings())
    assert [fake.kind(r) for r in fake.requests] == ["list"]


def test_a_failed_write_deletes_the_half_filled_sheet():
    fake = FakeDocs(errors=[(lambda k: k == "updateRangeRequest", {"code": 400001, "message": "bad cells"})])
    with pytest.raises(TencentDocsError, match="400001"):
        upload_table({"rows": [row(0)]}, settings(), sheet_title="t", client=fake.client())
    assert fake.bodies("deleteSheetRequest") == [{"sheetId": "NEW1"}]
    assert [s["title"] for s in fake.sheets] == ["工作表1"]


def test_a_backend_timeout_on_a_write_is_retried():
    flaky = {"code": 500001, "message": "read tcp: i/o timeout"}
    fake = FakeDocs(errors=[(lambda k: k == "updateRangeRequest", flaky)])
    url = upload_table({"rows": [row(0)]}, settings(), sheet_title="t", client=fake.client())
    assert url.endswith("?tab=NEW1")
    assert len(fake.bodies("updateRangeRequest")) == 2 and fake.bodies("deleteSheetRequest") == []


def test_creating_the_sheet_is_not_retried():
    flaky = {"code": 500001, "message": "i/o timeout"}
    fake = FakeDocs(errors=[(lambda k: k == "addSheetRequest", flaky)])
    with pytest.raises(TencentDocsError, match="500001"):
        upload_table({"rows": [row(0)]}, settings(), sheet_title="t", client=fake.client())
    assert len(fake.bodies("addSheetRequest")) == 1


# ---- settings ----

def write_config(tmp_path, tencent: dict | None) -> object:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({} if tencent is None else {"upload": {"tencent": tencent}}), encoding="utf-8")
    return path


ENV = {"TENCENT_DOCS_ACCESS_TOKEN": "t", "TENCENT_DOCS_CLIENT_ID": "c", "TENCENT_DOCS_OPEN_ID": "o"}


def test_credentials_come_from_env_named_in_config(tmp_path):
    from backend.tencent_docs import load_credentials

    cfg = write_config(tmp_path, {"access_token_env": "MY_TOKEN"})
    s = load_credentials(cfg, env={**ENV, "MY_TOKEN": "mine"})
    assert (s.access_token, s.client_id, s.open_id) == ("mine", "c", "o")


@pytest.mark.parametrize("drop", list(ENV))
def test_a_missing_credential_fails_fast_naming_it(tmp_path, drop):
    from backend.tencent_docs import load_credentials

    env = {k: v for k, v in ENV.items() if k != drop}
    with pytest.raises(TencentDocsError, match=drop):
        load_credentials(write_config(tmp_path, {}), env=env)


def test_no_upload_section_names_every_credential(tmp_path):
    from backend.tencent_docs import load_credentials

    with pytest.raises(TencentDocsError) as e:
        load_credentials(write_config(tmp_path, None), env={})
    assert all(name in str(e.value) for name in ENV)


# ---- CLI ----

def test_cli_uploads_an_archive_folder(tmp_path, monkeypatch, capsys):
    import backend.tencent_docs as td

    folder = tmp_path / "downloads" / "鹿乃まほろ" / "20261111" / "T"
    folder.mkdir(parents=True)
    (folder / "[20261111][鹿乃まほろ] T.highlights.json").write_text(
        json.dumps({"streamer": "鹿乃まほろ", "title": "T", "rows": [row(0)]}), encoding="utf-8")
    fake = FakeDocs()
    real_upload = td.upload_table
    monkeypatch.setattr(td, "upload_table", lambda *a, **kw: real_upload(*a, **kw, client=fake.client()))
    monkeypatch.setattr(td, "load_credentials", lambda cfg: settings(file_id=""))

    assert td.main([str(folder), "--sheet", "https://docs.qq.com/sheet/CLI?tab=x"]) == 0
    assert fake.bodies("addSheetRequest")[0]["title"] == "20261111 鹿乃まほろ T"
    out = capsys.readouterr().out
    assert "https://docs.qq.com/sheet/CLI?tab=NEW1" in out
    assert SECRETS["access_token"] not in out


def test_cli_reports_missing_settings(tmp_path, capsys):
    import backend.tencent_docs as td

    f = tmp_path / "x.highlights.json"
    f.write_text("{}", encoding="utf-8")
    cfg = write_config(tmp_path, {"access_token_env": "KIRI_TEST_NO_SUCH_TOKEN"})  # never in a real .env
    assert td.main([str(f), "--sheet", "FAKEFILE", "--config", str(cfg)]) == 1
    assert "KIRI_TEST_NO_SUCH_TOKEN" in capsys.readouterr().err
