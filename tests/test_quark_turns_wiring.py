"""The service holds one Quark object and gives it to the pipeline and to the group-files
module, so their uploads take turns; the deploy check and the by-hand commands build their
own as before. A group file's download does not wait for the drive."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from backend import quark
from backend.downloader.auto import load_auto
from backend.monitor.server import create_app

from .group_file_fakes import FOLDER, make, settle
from .helpers import wait_for
from .test_qq import SECRET
from .test_quark_turns import ZIP, Uploads, cli, drive_copy, root  # noqa: F401  (pytest fixtures)


def write_config(tmp_path, cli: bool = True, auto: bool = True):
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
        "download": {"output_dir": str(tmp_path / "dl"),
                     "auto": {"state_file": str(tmp_path / "queue.json"), "enabled": auto}},
        "drive": {"quark_cli": str(cli_file)},
    }), encoding="utf-8")
    return cfg


def app(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("KIRI_T_QQ_ID", "APP1")
    monkeypatch.setenv("KIRI_T_QQ_SECRET", SECRET)
    return create_app(write_config(tmp_path, **kw))


# ---- the service's one drive ----

def test_the_service_gives_one_quark_object_to_the_pipeline_and_the_group_files_module(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="backend.quark"):
        a = app(tmp_path, monkeypatch)

    assert isinstance(a.state.drive, quark.Quark)
    assert a.state.auto.drive is a.state.drive
    assert a.state.group_files.drive is a.state.drive
    assert len([r for r in caplog.records if r.message.startswith("drive available")]) == 1  # built once


def test_with_the_automatic_download_off_group_files_still_get_the_drive(tmp_path, monkeypatch):
    a = app(tmp_path, monkeypatch, auto=False)

    assert a.state.auto is None
    assert isinstance(a.state.group_files.drive, quark.Quark) and a.state.group_files.drive is a.state.drive


def test_without_the_cli_nobody_gets_a_drive(tmp_path, monkeypatch):
    a = app(tmp_path, monkeypatch, cli=False)

    assert a.state.drive is None and a.state.auto.drive is None and a.state.group_files.drive is None


def test_dev_mode_has_no_drive(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRI_T_QQ_ID", "APP1")
    monkeypatch.setenv("KIRI_T_QQ_SECRET", SECRET)
    assert create_app(write_config(tmp_path), dev=True).state.drive is None


def test_the_pipeline_takes_the_drive_it_is_given_and_builds_none_of_its_own(tmp_path, caplog):
    given = object()
    with caplog.at_level(logging.INFO, logger="backend.quark"):
        ad = load_auto(write_config(tmp_path), drive=given)

    assert ad.drive is given
    assert not [r for r in caplog.records if "drive" in r.message]


def test_the_pipeline_given_no_drive_has_none(tmp_path):
    assert load_auto(write_config(tmp_path), drive=None).drive is None


def test_left_alone_the_pipeline_has_no_drive(tmp_path, caplog):
    """The deploy check calls load_auto without a drive: it only reads the queue."""
    with caplog.at_level(logging.INFO, logger="backend.quark"):
        assert load_auto(write_config(tmp_path)).drive is None
    assert not [r for r in caplog.records if "drive" in r.message]


# ---- the group-files module with the real Quark module ----

def test_a_group_files_download_starts_while_a_drive_copy_uploads_and_its_upload_waits(tmp_path, root, cli):
    uploads = Uploads(cli)
    d = quark.Quark(root / "quark-drive.cjs", root)
    s = make(tmp_path, drive=d)
    copy = drive_copy(d, root)
    assert wait_for(lambda: uploads.started == [ZIP])

    s.post("【待校】第3话.ass", b"[Script Info]\n")
    assert asyncio.run(s.keeper.download_next())  # at once, whatever the drive is doing
    assert [p.name for p in s.temp.rglob("*.ass")] == ["【待校】第3话.ass"]

    worker = threading.Thread(target=lambda: asyncio.run(settle(s.keeper)), daemon=True)  # the upload loop
    worker.start()
    time.sleep(0.3)  # waiting for its turn: nothing listed yet
    assert uploads.started == [ZIP]
    assert ["create-folder", "--dir-path", f"{FOLDER}/待校", "--parent-fid", "0"] not in cli.commands()

    uploads.let_go(ZIP)
    uploads.let_go("【待校】第3话.ass")
    worker.join(5)
    assert copy.ended() and not worker.is_alive()
    assert uploads.started == [ZIP, "【待校】第3话.ass"]
    assert ["create-folder", "--dir-path", f"{FOLDER}/待校", "--parent-fid", "0"] in cli.commands()
