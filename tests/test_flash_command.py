"""The flash module's command `status`, against a fake flash service."""

from __future__ import annotations

import json

import pytest

from backend.flash.__main__ import main
from backend.flash.client import flash_configured, load_flash

from .flash_fakes import QR_CODE, FakeFlashService, closed_port


@pytest.fixture
def service():
    with FakeFlashService() as fake:
        yield fake


@pytest.fixture
def config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"download": {"output_dir": str(tmp_path / "downloads")}}), encoding="utf-8")
    return cfg


def status(config, env, capsys) -> tuple[int, str]:
    code = main(["status", "--config", str(config)], env=env)
    out = capsys.readouterr()
    return code, out.out + out.err


def test_status_says_when_no_flash_service_is_configured(config, capsys):
    code, out = status(config, {}, capsys)

    assert code == 2
    assert "FLASH_ACTIONS_URL" in out


def test_status_of_an_online_account(service, config, capsys):
    code, out = status(config, service.env(), capsys)

    assert code == 0
    assert out.strip() == "闪传账号：在线"


def test_status_of_an_offline_account_points_to_the_notify_page_without_printing_the_qr_code(service, config, capsys):
    service.online = False
    service.login_error = "二维码已过期"

    code, out = status(config, service.env(), capsys)

    assert code == 1
    assert len(out.strip().splitlines()) == 1
    assert "掉线" in out and "二维码已过期" in out and "「通知」页" in out
    assert QR_CODE not in out


def test_status_of_an_offline_account_without_the_login_hash_says_it_is_missing(service, config, capsys):
    service.online = False

    code, out = status(config, service.env(FLASH_LOGIN_HASH=None), capsys)

    assert code == 1
    assert "掉线" in out and "FLASH_LOGIN_HASH" in out
    assert len(out.strip().splitlines()) == 1


def test_status_when_the_flash_service_cannot_be_reached(config, capsys):
    code, out = status(config, {"FLASH_ACTIONS_URL": f"ws://127.0.0.1:{closed_port()}"}, capsys)

    assert code == 1
    assert out.strip() == "闪传账号：连不上闪传服务"


def test_status_when_the_flash_service_answers_an_error(service, config, capsys):
    service.answers["get_status"] = lambda params: {"data": None, "message": "内部错误"}

    code, out = status(config, service.env(), capsys)

    assert code == 1
    assert "读取失败" in out and "内部错误" in out


def test_the_settings_come_from_the_process_environment_and_the_downloads_folder_from_config(
        service, config, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.flash.client.load_dotenv", lambda path: None)  # no .env of the machine
    for name, value in service.env(FLASH_DOWNLOADS_PATH="/mnt/dl").items():
        monkeypatch.setenv(name, value)

    client = load_flash(config)

    assert client is not None and flash_configured()
    assert client.flash_path(tmp_path / "downloads" / "森野鈴" / "a.mp4") == "/mnt/dl/森野鈴/a.mp4"
    assert client.account().state == "online"
