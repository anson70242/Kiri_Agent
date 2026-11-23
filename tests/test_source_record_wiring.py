"""The service builds the source record sheet's module when QQ and the Tencent Docs token are
both there, hands it to the pipeline and runs its worker beside the others; without either,
the pipeline tells nothing. Through create_app with a fake Tencent Docs endpoint; made-up
group ids, file ids, tab ids and tokens."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.monitor import server
from backend.monitor.server import create_app
from backend.notify import EventKind
from backend.notify.qq import SourceRecordSheet
from backend.source_record_sheet import SourceRecordKeeper

from .helpers import A, B, bare_archive, wait_for
from .source_record_fakes import FILE_ID, TAB_ID, TOKEN, FakeTencentDocs, record_sheet, settle
from .test_auth import cli
from .test_drive_stage import downloaded
from .test_qq import SECRET

TOKEN_ENV = "KIRI_T_TENCENT"
SITE = "http://testserver"
SHEET = SourceRecordSheet(FILE_ID, TAB_ID)


def write_config(tmp_path, *, qq: bool = True, auth: bool = False):
    notify: dict = {"state_file": str(tmp_path / "state.json"), "log_file": str(tmp_path / "log.json")}
    if qq:
        notify["qq"] = {"app_id_env": "KIRI_T_QQ_ID", "secret_env": "KIRI_T_QQ_SECRET",
                        "groups_file": str(tmp_path / "data" / "qq_groups.json")}
    body = {
        "monitor": {"streamers": [{"name": "A", "platform": "youtube", "channel": "@a", "auto_download": True},
                                  {"name": "B", "platform": "twitch", "channel": "b", "auto_download": True}],
                    "streamers_file": str(tmp_path / "data" / "streamers.json")},
        "notify": notify,
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(tmp_path / "queue.json")}},
        "upload": {"tencent": {"token_env": TOKEN_ENV}},
    }
    if auth:
        body["auth"] = {"enabled": True}
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(body), encoding="utf-8")
    return cfg


def app(tmp_path, monkeypatch, *, token: bool = True, qq: bool = True, auth: bool = False, dev: bool = False):
    monkeypatch.setenv("KIRI_T_QQ_ID", "APP1")
    monkeypatch.setenv("KIRI_T_QQ_SECRET", SECRET)
    if token:
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
    else:
        monkeypatch.delenv(TOKEN_ENV, raising=False)
    cfg = write_config(tmp_path, qq=qq, auth=auth)
    if auth:
        assert cli(monkeypatch, cfg, "add-user", "boss", "--admin", "--password-stdin") == 0
    return create_app(cfg, dev=dev)


def test_with_qq_and_a_token_the_pipeline_tells_the_module_which_asks_the_qq_groups(tmp_path, monkeypatch):
    a = app(tmp_path, monkeypatch)
    keeper = a.state.source_record

    assert isinstance(keeper, SourceRecordKeeper)
    assert a.state.auto.source_record is keeper
    assert keeper.groups is a.state.notifier.qq.groups
    assert keeper.settings.token == TOKEN


def test_without_a_token_there_is_no_module_and_the_pipeline_tells_nothing(tmp_path, monkeypatch):
    a = app(tmp_path, monkeypatch, token=False)
    assert a.state.source_record is None and a.state.auto.source_record is None


def test_without_qq_there_is_no_module(tmp_path, monkeypatch):
    a = app(tmp_path, monkeypatch, qq=False)
    assert a.state.source_record is None and a.state.auto.source_record is None


def test_dev_mode_has_no_module(tmp_path, monkeypatch):
    assert app(tmp_path, monkeypatch, dev=True).state.source_record is None


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_a_processed_live_lands_in_the_groups_sheet_with_the_messages_short_download_link(tmp_path, monkeypatch):
    a = app(tmp_path, monkeypatch, auth=True)
    # 2026-11-22 23:30 in Hong Kong: the short download link expires 31 days later, on 2026-12-23
    # in Hong Kong while it is still 2026-12-23 15:30 in UTC.
    a.state.auth.download_links.clock = Clock(datetime(2026, 11, 22, 15, 30, tzinfo=timezone.utc).timestamp())
    groups = a.state.notifier.qq.groups
    groups.register("G1")
    groups.update("G1", name="向日葵字幕组", streamers=["A"], enabled=False, source_record_sheet=SHEET)
    docs = FakeTencentDocs()
    tab = record_sheet(docs)
    keeper = a.state.source_record
    keeper.client = docs.client()
    sent: list = []
    auto = a.state.auto
    auto.notify, auto.site_url = sent.append, SITE

    downloaded(auto, B)  # nobody records B
    asyncio.run(settle(keeper))
    assert docs.calls == []
    downloaded(auto, A)
    asyncio.run(settle(keeper))

    ready = [e for e in sent if e.kind is EventKind.READY][-1]
    assert ready.links[0].url.startswith(f"{SITE}/d/")
    assert tab.texts(2) == ["A", "2026/11/10", "录播", "title v1（有效至 2026/12/23）\n全部下载"]
    assert tab.rich(2, 6)[1] == {"text": "全部下载", "link_url": ready.links[0].url}


def test_archives_already_there_at_start_are_not_recorded(tmp_path, monkeypatch):
    bare_archive(tmp_path / "dl", "A", "20261110", "歌枠", stem="v", video=b"x", chat=[])  # processed before
    monkeypatch.setattr(server.QQGateway, "run", stay_offline)
    a = app(tmp_path, monkeypatch)
    a.state.notifier.qq.groups.register("G1")
    a.state.notifier.qq.groups.update("G1", streamers=["A"], source_record_sheet=SHEET)
    docs = FakeTencentDocs()
    record_sheet(docs)
    a.state.source_record.client = docs.client()
    with TestClient(a):
        assert not asyncio.run(a.state.source_record.write_next())
    assert docs.calls == []


async def stay_offline(self):  # the gateway would connect to QQ
    await asyncio.Event().wait()


def test_the_lifespan_runs_the_worker_and_stops_it_with_the_others(tmp_path, monkeypatch):
    events: list[str] = []

    async def worker(self):
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    monkeypatch.setattr(server.QQGateway, "run", stay_offline)
    monkeypatch.setattr(SourceRecordKeeper, "run", worker)
    monkeypatch.setattr(SourceRecordKeeper, "close", lambda self: events.append("closed"))
    a = app(tmp_path, monkeypatch)

    with TestClient(a):
        assert wait_for(lambda: events == ["started"])
    assert events == ["started", "stopped", "closed"]
