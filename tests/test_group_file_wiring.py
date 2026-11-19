"""The service builds the group-files module next to the QQ gateway, with the pipeline's
drive, the jobs file beside the QQ groups file and local copies on the downloads volume;
the gateway hands files to it; the lifespan runs its worker and stops it. Dev mode has none."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from backend.group_files import JOBS_FILE, GroupFileKeeper
from backend.monitor import server
from backend.monitor.server import create_app

from .helpers import wait_for
from .test_qq import SECRET


def write_config(tmp_path, cli: bool = True):
    cli_file = tmp_path / "quark" / "quark-drive.cjs"
    if cli:
        cli_file.parent.mkdir()
        cli_file.write_text("// the CLI", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [], "streamers_file": str(tmp_path / "data" / "streamers.json")},  # no network
        "notify": {"state_file": str(tmp_path / "state.json"), "log_file": str(tmp_path / "log.json"),
                   "qq": {"app_id_env": "KIRI_T_QQ_ID", "secret_env": "KIRI_T_QQ_SECRET",
                          "groups_file": str(tmp_path / "data" / "qq_groups.json")}},
        "download": {"output_dir": str(tmp_path / "dl"), "auto": {"state_file": str(tmp_path / "queue.json")}},
        "drive": {"quark_cli": str(cli_file)},
    }), encoding="utf-8")
    return cfg


def app(tmp_path, monkeypatch, cli: bool = True, dev: bool = False):
    monkeypatch.setenv("KIRI_T_QQ_ID", "APP1")
    monkeypatch.setenv("KIRI_T_QQ_SECRET", SECRET)
    return create_app(write_config(tmp_path, cli), dev=dev)


def test_the_module_gets_the_pipelines_drive_and_the_gateway_hands_files_to_it(tmp_path, monkeypatch):
    a = app(tmp_path, monkeypatch)
    keeper = a.state.group_files

    assert isinstance(keeper, GroupFileKeeper)
    assert keeper.drive is a.state.auto.drive and keeper.drive is not None
    assert keeper.jobs.path == tmp_path / "data" / JOBS_FILE
    assert keeper.temp == (tmp_path / "dl" / ".group_files").resolve()
    assert keeper.groups is a.state.notifier.qq.groups
    assert a.state.gateway.take_group_file == keeper.posted


def test_without_the_drive_the_module_is_there_and_ignores_files(tmp_path, monkeypatch):
    assert app(tmp_path, monkeypatch, cli=False).state.group_files.drive is None


def test_dev_mode_has_no_module(tmp_path, monkeypatch):
    assert app(tmp_path, monkeypatch, dev=True).state.group_files is None


def test_the_lifespan_runs_the_worker_and_stops_it_with_the_others(tmp_path, monkeypatch):
    events: list[str] = []

    async def stay_offline(self):  # the gateway would connect to QQ
        await asyncio.Event().wait()

    async def worker(self):
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    async def closed(self):
        events.append("closed")

    monkeypatch.setattr(server.QQGateway, "run", stay_offline)
    monkeypatch.setattr(GroupFileKeeper, "run", worker)
    monkeypatch.setattr(GroupFileKeeper, "aclose", closed)
    a = app(tmp_path, monkeypatch)

    with TestClient(a):
        assert wait_for(lambda: events == ["started"])
    assert events[:2] == ["started", "stopped"] and "closed" in events
