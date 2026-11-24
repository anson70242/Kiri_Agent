"""The flash service client against a fake flash service that speaks both of its wire
interfaces (the actions over a WebSocket, the login interface over HTTP)."""

from __future__ import annotations

import logging

import pytest

from backend.flash.client import FlashClient, FlashFailed, FlashSettings, FlashUnavailable, flash_configured

from .flash_fakes import CREDENTIAL, GROUP_TEXT, LOGIN_HASH, NEW_QR_CODE, QR_CODE, FakeFlashService, closed_port


@pytest.fixture
def service():
    with FakeFlashService() as fake:
        yield fake


@pytest.fixture
def downloads(tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    return folder


# ---- configured or not ----

def test_a_flash_service_is_configured_only_with_the_address_of_its_actions():
    assert flash_configured({"FLASH_ACTIONS_URL": "ws://flash.example:8001"})
    assert not flash_configured({})
    assert not flash_configured({"FLASH_ACTIONS_URL": "  "})
    assert not flash_configured({"FLASH_LOGIN_URL": "http://flash.example:8002", "FLASH_LOGIN_HASH": "x"})


def test_the_downloads_path_of_the_flash_service_defaults_to_data_downloads():
    settings = FlashSettings.from_env({"FLASH_ACTIONS_URL": "ws://flash.example:8001"})
    assert settings is not None and settings.downloads_path == "/data/downloads"


# ---- the account's state ----

def test_an_online_account(service, downloads):
    state = FlashClient(service.settings(), downloads).account()

    assert state.state == "online"
    assert state.qr_code is None


def test_an_unreachable_flash_service(downloads):
    settings = FlashSettings.from_env({"FLASH_ACTIONS_URL": f"ws://127.0.0.1:{closed_port()}"})

    assert FlashClient(settings, downloads).account().state == "unreachable"


def test_an_offline_account_gives_the_qr_code_and_the_flash_services_own_error(service, downloads):
    service.online = False
    service.login_error = "二维码已过期"

    state = FlashClient(service.settings(), downloads).account()

    assert state.state == "offline"
    assert state.qr_code == QR_CODE
    assert state.error == "二维码已过期"
    assert state.missing == ""
    assert service.login_calls[0] == ("/api/auth/login", None, {"hash": LOGIN_HASH})
    assert all(auth == f"Bearer {CREDENTIAL}" for _, auth, _ in service.login_calls[1:])


@pytest.mark.parametrize("left_out, says", [("FLASH_LOGIN_HASH", "FLASH_LOGIN_HASH"), ("FLASH_LOGIN_URL", "FLASH_LOGIN_URL")])
def test_without_the_login_interface_the_state_comes_from_get_status_alone_and_says_what_is_missing(
        service, downloads, left_out, says):
    settings = service.settings(**{left_out: None})
    client = FlashClient(settings, downloads)

    assert client.account().state == "online"
    service.online = False
    state = client.account()
    assert state.state == "offline"
    assert state.qr_code is None
    assert says in state.missing
    assert service.login_calls == []


def test_an_offline_account_whose_login_interface_cannot_be_reached_is_offline_with_no_qr_code(service, downloads):
    service.online = False
    settings = service.settings(FLASH_LOGIN_URL=f"http://127.0.0.1:{closed_port()}")

    state = FlashClient(settings, downloads).account()

    assert state.state == "offline" and state.qr_code is None
    assert "登录接口" in state.error


def test_a_login_hash_the_login_interface_refuses_is_offline_with_its_words(service, downloads):
    service.online = False
    settings = service.settings(FLASH_LOGIN_HASH="not-the-hash")

    state = FlashClient(settings, downloads).account()

    assert state.state == "offline" and state.qr_code is None
    assert "密钥不对" in state.error


# ---- a new QR code ----

def test_asking_for_a_new_qr_code_gives_the_new_one_in_the_next_state(service, downloads):
    service.online = False
    client = FlashClient(service.settings(), downloads)

    client.refresh_qr_code()

    assert service.qr_refreshes == 1
    assert client.account().qr_code == NEW_QR_CODE


def test_asking_for_a_new_qr_code_while_logged_in_fails_with_the_flash_services_words(service, downloads):
    service.logged_in = True

    with pytest.raises(FlashFailed, match="已经登录了"):
        FlashClient(service.settings(), downloads).refresh_qr_code()


def test_asking_for_a_new_qr_code_without_the_login_interface_fails_saying_what_is_missing(service, downloads):
    with pytest.raises(FlashFailed, match="FLASH_LOGIN_HASH"):
        FlashClient(service.settings(FLASH_LOGIN_HASH=None), downloads).refresh_qr_code()
    assert service.login_calls == []


def test_asking_for_a_new_qr_code_from_an_unreachable_login_interface_is_unavailable(service, downloads):
    settings = service.settings(FLASH_LOGIN_URL=f"http://127.0.0.1:{closed_port()}")

    with pytest.raises(FlashUnavailable):
        FlashClient(settings, downloads).refresh_qr_code()


# ---- waiting or failing ----

@pytest.mark.parametrize("online", [True, False])
def test_waiting_for_the_account_raises_its_own_error_only_while_offline(service, downloads, online):
    service.online = online
    client = FlashClient(service.settings(), downloads)

    if online:
        client.require_online()
    else:
        with pytest.raises(FlashUnavailable):
            client.require_online()


def test_an_unreachable_flash_service_is_waited_for_like_an_offline_account(downloads):
    settings = FlashSettings.from_env({"FLASH_ACTIONS_URL": f"ws://127.0.0.1:{closed_port()}"})

    with pytest.raises(FlashUnavailable):
        FlashClient(settings, downloads).require_online()


def test_an_action_answered_with_an_error_is_a_failure_with_the_flash_services_words(service, downloads):
    service.answers["get_status"] = lambda params: {"data": None, "message": "内部错误"}
    client = FlashClient(service.settings(), downloads)

    with pytest.raises(FlashFailed, match="内部错误"):
        client.account()
    with pytest.raises(FlashFailed, match="内部错误"):
        client.require_online()


def test_event_frames_and_answers_to_other_callers_are_passed_over(service, downloads):
    service.events = service.events * 50  # among them an answer with another echo saying offline

    assert FlashClient(service.settings(), downloads).account().state == "online"
    assert [frame["action"] for frame in service.actions] == ["get_status"]
    assert set(service.actions[0]) == {"action", "params", "echo"}


# ---- paths as the flash service sees them ----

def test_a_path_under_the_downloads_folder_is_given_as_the_flash_service_sees_it(service, downloads):
    video = downloads / "森野鈴" / "[20261122][森野鈴] 标题" / "[20261122][森野鈴] 标题.mp4"
    client = FlashClient(service.settings(FLASH_DOWNLOADS_PATH="/mnt/kiri-downloads/"), downloads)

    assert client.flash_path(video) == "/mnt/kiri-downloads/森野鈴/[20261122][森野鈴] 标题/[20261122][森野鈴] 标题.mp4"


def test_the_flash_service_sees_the_downloads_folder_at_data_downloads_by_default(service, downloads):
    client = FlashClient(service.settings(FLASH_DOWNLOADS_PATH=None), downloads)

    assert client.flash_path(downloads / "森野鈴" / "a.mp4") == "/data/downloads/森野鈴/a.mp4"


@pytest.mark.parametrize("outside", ["elsewhere/a.mp4", "downloads/../elsewhere/a.mp4", "downloads"])
def test_a_path_outside_the_downloads_folder_is_refused(service, downloads, outside):
    client = FlashClient(service.settings(), downloads)

    with pytest.raises(FlashFailed):
        client.flash_path(downloads.parent / outside)


# ---- nothing of the flash service in the log ----

def test_no_address_hash_credential_qr_code_or_group_message_is_logged_even_at_debug_level(service, downloads, caplog):
    secrets = [service.actions_url, service.login_url, "127.0.0.1", LOGIN_HASH, CREDENTIAL, QR_CODE, NEW_QR_CODE,
               GROUP_TEXT, "made-up-token"]
    unreachable = FlashSettings.from_env({"FLASH_ACTIONS_URL": f"ws://127.0.0.1:{closed_port()}/?access_token=made-up-token"})
    errors: list[str] = []
    caplog.set_level(logging.DEBUG)

    client = FlashClient(service.settings(), downloads)
    client.account()
    service.online = False
    client.account()
    client.refresh_qr_code()
    client.account()
    service.logged_in = True
    for failing in (client.refresh_qr_code, FlashClient(unreachable, downloads).require_online,
                    FlashClient(service.settings(FLASH_LOGIN_URL=f"http://127.0.0.1:{closed_port()}"), downloads).refresh_qr_code):
        with pytest.raises((FlashFailed, FlashUnavailable)) as e:
            failing()
        errors.append(str(e.value))
    errors.append(FlashClient(unreachable, downloads).account().error)

    for secret in secrets:
        assert secret not in caplog.text
        assert not any(secret in error for error in errors)


# ---- the credential of the login interface ----

def logins(service) -> int:
    return sum(path == "/api/auth/login" for path, _, _ in service.login_calls)


def test_the_login_interface_is_logged_in_to_once_for_many_looks_at_an_offline_account(service, downloads):
    """The 通知 page asks every few seconds while a QR code shows, and the login interface
    refuses more than a few logins a minute."""
    service.online = False
    client = FlashClient(service.settings(), downloads)

    states = [client.account() for _ in range(5)]
    client.refresh_qr_code()

    assert all(state.qr_code == QR_CODE for state in states)
    assert client.account().qr_code == NEW_QR_CODE
    assert logins(service) == 1


def test_a_credential_the_login_interface_no_longer_takes_is_replaced_by_logging_in_again(service, downloads):
    service.online = False
    client = FlashClient(service.settings(), downloads)
    assert client.account().qr_code == QR_CODE

    service.credential = "made-up-credential-after-an-hour"  # the old one has run out

    state = client.account()
    assert state.qr_code == QR_CODE and state.error == ""
    assert client.account().qr_code == QR_CODE
    assert logins(service) == 2


def test_a_refusal_that_logging_in_again_does_not_cure_is_tried_once_and_told(service, downloads):
    service.logged_in = True
    client = FlashClient(service.settings(), downloads)
    with pytest.raises(FlashFailed, match="已经登录了"):
        client.refresh_qr_code()

    with pytest.raises(FlashFailed, match="已经登录了"):
        client.refresh_qr_code()

    assert logins(service) == 2  # the first press's, and one more to rule the credential out
