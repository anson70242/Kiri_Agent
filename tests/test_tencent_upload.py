"""Uploading a 路灯表 to Tencent Docs, against a fake docs.qq.com MCP endpoint (httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from backend.sheet import HEADER
from backend.tencent_docs import TencentDocsError, TencentSettings, upload_table

from .helpers import bare_archive

FILE_ID = "FAKEFILE"
TOKEN = "tok-SECRET-123"


def settings(**kw) -> TencentSettings:
    return TencentSettings(**{"token": TOKEN, "file_id": FILE_ID, "retry_delay": 0, **kw})


def row(i: int, **kw) -> dict:
    return {"start": f"00:{i:02d}:00", "end": f"00:{i + 5:02d}:00", "topics": ["杂谈", "游戏"],
            "timeline": [{"start": "00:00:00", "end": "00:02:00", "title": "【开场】", "detail": "打招呼"},
                         {"start": "00:02:00", "end": "00:05:00", "title": "【猫】", "detail": "猫来了"}],
            "highlights": [], "score": 5, "chat_level": "中", **kw}


class FakeDocs:
    """Records every MCP tool call; answers like docs.qq.com/openapi/mcp (JSON-RPC over HTTP)."""

    def __init__(self, sheets=("工作表1",), errors=None, rows=200):
        self.sheets = [{"sheet_id": f"s{i}", "sheet_name": t, "row_count": rows} for i, t in enumerate(sheets)]
        self.calls: list[tuple[str, dict]] = []
        self.requests: list[httpx.Request] = []
        self.errors = dict(errors or {})  # tool -> JSON-RPC error message, answered once
        self.expired = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.expired:
            return httpx.Response(401, json={"error": "invalid_token", "error_description": "invalid or expired token"})
        body = json.loads(request.content)
        assert body["method"] == "tools/call"
        tool, args = body["params"]["name"], body["params"]["arguments"]
        assert args["file_id"] == FILE_ID
        self.calls.append((tool, args))
        if msg := self.errors.pop(tool, None):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32603, "message": msg}})
        result: dict = {"error": "", "trace_id": "t"}
        if tool == "sheet.get_sheet_info":
            result["sheets"] = self.sheets
        elif tool == "sheet.add_sheet":
            new = {"sheet_id": "NEW1", "sheet_name": args["name"], "row_count": 200}
            self.sheets.insert(args.get("index", len(self.sheets)), new)
            result["sheet_id"] = "NEW1"
        elif tool == "sheet.delete_sheet":
            self.sheets = [s for s in self.sheets if s["sheet_id"] != args["sheet_id"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]

    def args(self, tool: str) -> list[dict]:
        return [a for t, a in self.calls if t == tool]

    def written(self) -> list[list]:
        """All set_range_value cells as plain values, laid out as a grid."""
        grid: dict[tuple[int, int], object] = {}
        for a in self.args("sheet.set_range_value"):
            for v in a["values"]:
                key = {"NUMBER": "number_value", "STRING": "string_value"}[v["value_type"].upper()]
                grid[v["row"], v["col"]] = v[key]
        rows = 1 + max(r for r, _ in grid)
        return [[grid.get((r, c), "") for c in range(len(HEADER))] for r in range(rows)]

    def style(self, r: int, c: int) -> dict:
        """The style one cell ends up with. Like the real set_cell_style, the last call
        covering a cell replaces its whole style: fields it leaves out go back to default
        (seen live: a later font colour wiped the cell's alignment and wrapping)."""
        out: dict = {}
        for a in self.args("sheet.set_cell_style"):
            if a["start_row"] <= r <= a["end_row"] and a["start_col"] <= c <= a["end_col"]:
                out = {k: v for k, v in a.items() if k not in
                       ("file_id", "sheet_id", "start_row", "end_row", "start_col", "end_col")}
        return out


def table3() -> dict:
    return {"streamer": "鹿乃まほろ", "title": "T", "rows": [
        row(0, score=8, chat_level="高", top_pick={"start": "00:01:00", "end": "00:02:00", "title": "名场面"}),
        row(5, score=5),
        row(10, score=3, chat_level="低"),
    ]}


def test_upload_adds_a_first_sheet_with_the_table_and_its_formatting():
    fake = FakeDocs(sheets=["工作表1", "20261108 X Y"])
    url = upload_table(table3(), settings(), sheet_title="20261111 鹿乃まほろ T", client=fake.client())

    assert url == f"https://docs.qq.com/sheet/{FILE_ID}?tab=NEW1"
    (add,) = fake.args("sheet.add_sheet")
    assert add["name"] == "20261111 鹿乃まほろ T" and add["index"] == 0  # newest first
    assert [s["sheet_name"] for s in fake.sheets][0] == "20261111 鹿乃まほろ T"
    assert all(a["sheet_id"] == "NEW1" for t, a in fake.calls if t not in ("sheet.get_sheet_info", "sheet.add_sheet"))
    for r in fake.requests:
        assert r.headers["Authorization"] == TOKEN
        assert str(r.url) == "https://docs.qq.com/openapi/mcp"

    grid = fake.written()
    assert grid[0] == HEADER and len(grid) == 4
    assert grid[1][2] == "杂谈\n游戏"
    assert grid[1][3] == "00:00:00–00:02:00 【开场】 打招呼\n\n00:02:00–00:05:00 【猫】 猫来了"
    assert grid[1][4] == "🏆 00:01:00–00:02:00 名场面"
    assert grid[1][5] == 8 and grid[3][5] == 3  # scores as numbers, so they sort and filter

    # Same look as the Excel download.
    for c in range(8):  # the whole header, 剪辑 included
        head = fake.style(0, c)
        assert head["bold"] and head["bg_color"].endswith("E8EFFB") and head["vertical_align"] == "center"
    assert fake.style(0, 0)["horizontal_align"] == "center"
    for r in (1, 2, 3):  # every body cell keeps its full look, coloured or not
        for c in range(8):
            s = fake.style(r, c)
            assert s["vertical_align"] == "center" and s["wrap_text"], (r, c)
            want = "left" if c in (3, 4) else "general" if c == 7 else "center"
            assert s["horizontal_align"] == want, (r, c)
    for c in range(8):
        assert fake.style(1, c)["bg_color"].endswith("FFFBEB")  # the 🏆 row
        assert "bg_color" not in fake.style(2, c)
    assert fake.style(1, 5)["font_color"].endswith("E11D48")  # score 8
    assert fake.style(2, 5)["font_color"].endswith("C2410C")  # score 5
    assert fake.style(3, 5)["font_color"].endswith("5A6B8C")  # score 3
    assert fake.style(1, 6)["bold"] and fake.style(1, 6)["font_color"].endswith("E11D48")  # chat 高
    assert "font_color" not in fake.style(3, 6)
    # Few calls: neighbouring cells with the same look share one.
    assert len(fake.args("sheet.set_cell_style")) <= 16
    widths = {d["index"]: d["size"] for a in fake.args("sheet.set_dimension_size") for d in a["dimensions"]
              if d["dimension_type"] == "col"}
    assert widths[3] > widths[4] > max(w for c, w in widths.items() if c not in (3, 4))
    assert fake.args("sheet.set_freeze")[0]["row_count"] == 1


def test_uploading_again_replaces_the_old_sheet():
    fake = FakeDocs(sheets=["工作表1", "20261111 A T"])
    upload_table({"rows": [row(0)]}, settings(), sheet_title="20261111 A T", client=fake.client())
    assert fake.tools()[:3] == ["sheet.get_sheet_info", "sheet.delete_sheet", "sheet.add_sheet"]
    assert fake.args("sheet.delete_sheet")[0]["sheet_id"] == "s1"
    assert sorted(s["sheet_name"] for s in fake.sheets) == ["20261111 A T", "工作表1"]


def test_the_only_sheet_is_never_deleted():
    # A spreadsheet can't be left with no sheet: keep it and number the new one.
    fake = FakeDocs(sheets=["20261111 A T"])
    upload_table({"rows": [row(0)]}, settings(), sheet_title="20261111 A T", client=fake.client())
    assert fake.args("sheet.delete_sheet") == []
    assert fake.args("sheet.add_sheet")[0]["name"] == "20261111 A T (2)"


def test_a_long_stream_gets_more_rows():
    # A new sheet has 200 rows; a 20-hour stream needs 241 (header + 240 segments).
    fake = FakeDocs()
    upload_table({"rows": [row(i % 50) for i in range(240)]}, settings(), sheet_title="t", client=fake.client())
    (ins,) = fake.args("sheet.insert_dimension")
    assert ins["dimension_type"] == "row" and ins["count"] >= 41
    assert len(fake.written()) == 241


def test_an_expired_token_is_a_clear_error_and_never_shows_the_token():
    fake = FakeDocs()
    fake.expired = True
    with pytest.raises(TencentDocsError) as e:
        upload_table({"rows": [row(0)]}, settings(), sheet_title="t", client=fake.client())
    assert "TENCENT_DOCS_MCP_TOKEN" in str(e.value)  # says what to renew
    assert TOKEN not in str(e.value) and TOKEN not in repr(settings())


def test_a_failed_write_deletes_the_half_filled_sheet():
    msg = "tool execution failed (tool: sheet.set_range_value): type:business, code:608651, msg:bad cells"
    fake = FakeDocs(errors={"sheet.set_range_value": msg})
    with pytest.raises(TencentDocsError, match="608651"):
        upload_table({"rows": [row(0)]}, settings(), sheet_title="t", client=fake.client())
    assert fake.args("sheet.delete_sheet") == [{"file_id": FILE_ID, "sheet_id": "NEW1"}]
    assert [s["sheet_name"] for s in fake.sheets] == ["工作表1"]


def test_sheet_title_is_date_creator_title_trimmed_and_safe():
    from backend.tencent_docs import MAX_TITLE, sheet_title, unique_title

    t = sheet_title({"streamer": "鹿乃まほろ", "title": "【Potato Please】60,000個のじゃがいもの皮って何時間で剥けますか？【鹿乃まほろ/ミリプロ】"},
                    "20261111")
    assert t.startswith("20261111 鹿乃まほろ 【Potato Please】") and len(t) <= MAX_TITLE
    assert "/" not in sheet_title({"title": "a/b[c]:d"})
    long = "x" * MAX_TITLE
    assert unique_title(long, {long}) == "x" * (MAX_TITLE - 4) + " (2)"


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


# ---- settings ----

def write_config(tmp_path, tencent: dict | None) -> object:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({} if tencent is None else {"upload": {"tencent": tencent}}), encoding="utf-8")
    return path


def test_the_token_comes_from_the_env_var_named_in_config(tmp_path):
    from backend.tencent_docs import load_credentials

    cfg = write_config(tmp_path, {"token_env": "MY_TOKEN"})
    assert load_credentials(cfg, env={"MY_TOKEN": "mine"}).token == "mine"
    assert load_credentials(write_config(tmp_path, None), env={"TENCENT_DOCS_MCP_TOKEN": "t"}).token == "t"


def test_a_missing_token_fails_fast_naming_it(tmp_path):
    from backend.tencent_docs import load_credentials

    with pytest.raises(TencentDocsError, match="TENCENT_DOCS_MCP_TOKEN"):
        load_credentials(write_config(tmp_path, {}), env={})


# ---- CLI ----

def test_cli_uploads_an_archive_folder(tmp_path, monkeypatch, capsys):
    import backend.tencent_docs as td

    folder = bare_archive(tmp_path / "downloads", "鹿乃まほろ", "20261111", "T",
                          highlights={"streamer": "鹿乃まほろ", "title": "T", "rows": [row(0)]})
    fake = FakeDocs()
    real_upload = td.upload_table
    monkeypatch.setattr(td, "upload_table", lambda *a, **kw: real_upload(*a, **kw, client=fake.client()))
    monkeypatch.setattr(td, "load_credentials", lambda cfg: settings(file_id=""))

    assert td.main([str(folder), "--sheet", f"https://docs.qq.com/sheet/{FILE_ID}?tab=x"]) == 0
    assert fake.args("sheet.add_sheet")[0]["name"] == "20261111 鹿乃まほろ T"
    out = capsys.readouterr().out
    assert f"https://docs.qq.com/sheet/{FILE_ID}?tab=NEW1" in out
    assert TOKEN not in out


def test_cli_reports_missing_settings(tmp_path, capsys):
    import backend.tencent_docs as td

    f = tmp_path / "x.highlights.json"
    f.write_text("{}", encoding="utf-8")
    cfg = write_config(tmp_path, {"token_env": "KIRI_TEST_NO_SUCH_TOKEN"})  # never in a real .env
    assert td.main([str(f), "--sheet", "FAKEFILE", "--config", str(cfg)]) == 1
    assert "KIRI_TEST_NO_SUCH_TOKEN" in capsys.readouterr().err
