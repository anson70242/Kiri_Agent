"""The 通知 page's 「闪传账号」 endpoints with a fake flash service client: the account's state
(在线 / 掉线 / 连不上闪传服务 / 未设置), the QR code drawn as an image, a new QR code, admins only.
No flash service is called; the QR codes are made up."""

from __future__ import annotations

import base64
import logging

import pytest
from fastapi.testclient import TestClient

from backend.flash.client import AccountState, FlashFailed, FlashUnavailable
from backend.monitor.server import create_app

from .test_auth import login, make_client
from .test_notify_api import write_config

QR_CODE = "https://qr.example/login?k=made-up-qr-0001"
NEW_QR_CODE = "https://qr.example/login?k=made-up-qr-0002"


class FakeFlashClient:
    """The flash service client as the dashboard uses it: the account's state, a new QR code.
    `state` is what `account()` answers; `refresh` what a new QR code does (None: the next
    state shows NEW_QR_CODE)."""

    def __init__(self, state: AccountState):
        self.state = state
        self.refresh: Exception | None = None
        self.refreshes = 0

    def account(self) -> AccountState:
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    def refresh_qr_code(self) -> None:
        self.refreshes += 1
        if self.refresh:
            raise self.refresh
        self.state = AccountState("offline", qr_code=NEW_QR_CODE)


def use_flash(monkeypatch, fake: FakeFlashClient | None) -> None:
    """The dashboard built next finds `fake` as its flash service (None: none configured)."""
    monkeypatch.setattr("backend.monitor.server.load_flash", lambda config: fake)


@pytest.fixture
def flash(monkeypatch):
    fake = FakeFlashClient(AccountState("online"))
    use_flash(monkeypatch, fake)
    return fake


def app(tmp_path) -> TestClient:
    return TestClient(create_app(write_config(tmp_path)))


def account(client: TestClient) -> dict:
    r = client.get("/api/notify/flash")
    assert r.status_code == 200
    return r.json()


def test_without_a_flash_service_the_block_says_not_set_up(tmp_path, monkeypatch):
    use_flash(monkeypatch, None)
    body = account(app(tmp_path))
    assert body["state"] == "unconfigured" and body["qr_image"] is None
    r = app(tmp_path).post("/api/notify/flash/qr-code")
    assert r.status_code == 404 and "FLASH_ACTIONS_URL" in r.json()["detail"]


def test_an_online_account(tmp_path, flash):
    body = account(app(tmp_path))
    assert body["state"] == "online" and body["qr_image"] is None


def svg(data_uri: str) -> str:
    """The image a `data:` URI holds, as text (an SVG)."""
    head, _, payload = data_uri.partition(",")
    assert head.startswith("data:image/svg+xml")
    if head.endswith(";base64"):
        return base64.b64decode(payload).decode()
    from urllib.parse import unquote
    return unquote(payload)


def test_an_offline_account_shows_its_qr_code_as_an_image(tmp_path, flash):
    flash.state = AccountState("offline", qr_code=QR_CODE)
    body = account(app(tmp_path))
    assert body["state"] == "offline" and body["error"] == "" and body["missing"] == ""
    drawn = svg(body["qr_image"])
    assert drawn.lstrip().startswith(("<svg", "<?xml")) and "<path" in drawn
    assert QR_CODE not in body["qr_image"] and QR_CODE not in drawn  # drawn, not passed on as text


def test_the_flash_service_s_own_error_text_is_shown(tmp_path, flash):
    flash.state = AccountState("offline", qr_code=QR_CODE, error="二维码已过期")
    body = account(app(tmp_path))
    assert body["state"] == "offline" and body["error"] == "二维码已过期" and body["qr_image"]


def test_without_the_login_settings_the_block_says_what_is_missing_and_shows_no_qr_code(tmp_path, flash):
    flash.state = AccountState("offline", missing="没有设置登录哈希（.env 里的 FLASH_LOGIN_HASH），看不到二维码")
    body = account(app(tmp_path))
    assert body["state"] == "offline" and body["qr_image"] is None
    assert "FLASH_LOGIN_HASH" in body["missing"]


def test_a_flash_service_that_cannot_be_reached_shows_no_qr_code(tmp_path, flash):
    flash.state = AccountState("unreachable")
    body = account(app(tmp_path))
    assert body["state"] == "unreachable" and body["qr_image"] is None


def test_a_flash_service_that_answers_an_error_counts_as_unreachable_with_its_words(tmp_path, flash):
    flash.state = FlashFailed("get_status：内部错误")
    body = account(app(tmp_path))
    assert body["state"] == "unreachable" and body["qr_image"] is None and "内部错误" in body["error"]


def test_a_new_qr_code_is_asked_for_and_shown(tmp_path, flash):
    flash.state = AccountState("offline", qr_code=QR_CODE)
    client = app(tmp_path)
    old = account(client)["qr_image"]

    r = client.post("/api/notify/flash/qr-code")

    assert r.status_code == 200 and flash.refreshes == 1
    body = r.json()
    assert body["state"] == "offline" and body["qr_image"] and body["qr_image"] != old
    assert account(client)["qr_image"] == body["qr_image"]


@pytest.mark.parametrize(("error", "status"), [(FlashUnavailable("连不上闪传服务的登录接口（URLError）"), 503),
                                               (FlashFailed("登录接口：已经登录"), 502)])
def test_a_new_qr_code_that_cannot_be_had_says_why(tmp_path, flash, error, status):
    flash.state = AccountState("offline", qr_code=QR_CODE)
    flash.refresh = error
    r = app(tmp_path).post("/api/notify/flash/qr-code")
    assert r.status_code == status and r.json()["detail"] == str(error)


def test_the_qr_code_is_in_no_log_line(tmp_path, flash, caplog):
    caplog.set_level(logging.DEBUG)
    flash.state = AccountState("offline", qr_code=QR_CODE)
    client = app(tmp_path)
    image = account(client)["qr_image"]
    new = client.post("/api/notify/flash/qr-code").json()["qr_image"]
    for secret in (QR_CODE, NEW_QR_CODE, image, new):
        assert secret not in caplog.text


def test_only_admins_see_the_flash_account_or_ask_for_a_new_qr_code(tmp_path, monkeypatch, flash):
    flash.state = AccountState("offline", qr_code=QR_CODE)
    client = make_client(tmp_path, monkeypatch)
    login(client, "fan")
    for r in (client.get("/api/notify/flash"), client.post("/api/notify/flash/qr-code")):
        assert r.status_code == 403 and r.json() == {"detail": "没有权限"}
    assert flash.refreshes == 0

    login(client, "boss")
    assert client.get("/api/notify/flash").json()["state"] == "offline"
    assert client.post("/api/notify/flash/qr-code").status_code == 200
