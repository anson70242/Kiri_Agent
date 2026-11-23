"""A flash service address setting that is not a usable address (no scheme, a bad port, a
broken host) comes out as the client's error for failures, with a text that holds no address,
and the 通知 page's 「闪传账号」 endpoints answer as for any other failure; nothing of the
address reaches the log. The client against the fake flash service, the dashboard's HTTP API
with the real client."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from backend.flash.client import FlashClient, FlashFailed
from backend.monitor.server import create_app

from .flash_fakes import FakeFlashService
from .test_notify_api import write_config

BAD_LOGIN_URLS = ["flash.example", "flash.example:8002", "http://[flash.example", "http://flash.example:80o2"]
BAD_ACTIONS_URLS = ["ws://[flash.example", "ws://flash.example:80o1"]


@pytest.fixture
def service():
    with FakeFlashService() as fake:
        fake.online = False  # the login interface is asked for the QR code
        yield fake


@pytest.fixture
def downloads(tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    return folder


@pytest.mark.parametrize("url", BAD_LOGIN_URLS)
def test_a_malformed_login_address_is_an_error_without_the_address(service, downloads, url, caplog):
    caplog.set_level(logging.DEBUG)
    client = FlashClient(service.settings(FLASH_LOGIN_URL=url), downloads)

    state = client.account()
    with pytest.raises(FlashFailed) as refused:
        client.refresh_qr_code()

    assert state.state == "offline" and state.qr_code is None
    assert "flash.example" not in state.error and "flash.example" not in str(refused.value)
    assert "flash.example" not in caplog.text


@pytest.mark.parametrize("url", BAD_ACTIONS_URLS)
def test_a_malformed_actions_address_is_an_error_without_the_address(downloads, url, caplog):
    from backend.flash.client import FlashSettings

    caplog.set_level(logging.DEBUG)
    client = FlashClient(FlashSettings.from_env({"FLASH_ACTIONS_URL": url}), downloads)

    with pytest.raises(FlashFailed) as failed:
        client.require_online()

    assert "flash.example" not in str(failed.value) and "flash.example" not in caplog.text


@pytest.mark.parametrize("url", BAD_LOGIN_URLS)
def test_the_notify_pages_endpoints_answer_a_malformed_login_address_as_another_failure(
        tmp_path, service, monkeypatch, url, caplog):
    caplog.set_level(logging.DEBUG)
    client = FlashClient(service.settings(FLASH_LOGIN_URL=url), tmp_path)
    monkeypatch.setattr("backend.monitor.server.load_flash", lambda config: client)
    app = TestClient(create_app(write_config(tmp_path)), raise_server_exceptions=False)

    state = app.get("/api/notify/flash")
    refresh = app.post("/api/notify/flash/qr-code")

    assert state.status_code == 200 and state.json()["state"] == "offline"
    assert refresh.status_code == 502
    assert "flash.example" not in state.text + refresh.text + caplog.text
