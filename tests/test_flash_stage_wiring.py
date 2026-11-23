"""The flash service in the running service: the pipeline gets the client of the configured
flash service (none without one), the same client the 通知 page asks; and the deploy check
does not hold a deploy for a flash transfer. Through `load_auto`, the dashboard built from a
written config, and the deploy check's command line."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend import check_state
from backend.downloader.auto import load_auto
from backend.flash.client import AccountState, FlashClient, load_flash
from backend.monitor.server import create_app

from .flash_stage_fakes import processed_live, stages, uploading, FakeFlash
from .test_drive_stage import step
from .test_flash_account_api import FakeFlashClient, use_flash
from .test_notify_api import write_config


def test_the_pipeline_gets_the_configured_flash_service_and_none_without_one(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    assert load_auto(cfg).flash is None
    assert load_auto(cfg, flash=load_flash(cfg)).flash is None  # the tests' environment configures none

    monkeypatch.setenv("FLASH_ACTIONS_URL", "ws://flash.example:3001")
    client = load_flash(cfg)
    assert isinstance(client, FlashClient)
    assert load_auto(cfg, flash=client).flash is client


def test_the_pipeline_and_the_notify_page_share_one_flash_service_client(tmp_path, monkeypatch):
    fake = FakeFlashClient(AccountState("online"))
    use_flash(monkeypatch, fake)
    monkeypatch.setenv("KIRI_T_HOOK", "https://discord.test/hook")
    client = TestClient(create_app(write_config(tmp_path)))

    assert client.app.state.auto.flash is fake
    assert client.get("/api/notify/flash").json()["state"] == "online"


def test_the_deploy_check_is_not_held_by_a_flash_transfer(tmp_path, capsys):
    ad, _, _, _ = processed_live(tmp_path / "pipeline", FakeFlash(uploading()))
    step(ad, "flash")  # uploading
    assert stages(ad) == ["flash"]
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "monitor": {"streamers": [], "streamers_file": str(tmp_path / "streamers.json")},
        "download": {"output_dir": str(tmp_path / "pipeline" / "dl"),
                     "auto": {"state_file": str(tmp_path / "pipeline" / "queue.json")}},
    }), encoding="utf-8")

    code = check_state.main(["--config", str(cfg), "--require-idle"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "下载队列：0 个任务。" in out
