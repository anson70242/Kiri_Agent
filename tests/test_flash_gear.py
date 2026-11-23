"""The gear's "自动上传到 QQ 闪传": the flash box through /api/streamers. Nothing is
transferred here: the gear ends at the roster's `flash` and the list of ticked streamers.

"A flash service is configured" is FLASH_ACTIONS_URL in the environment (`.env` once the
service has loaded it); the API asks at each request, so a test sets or removes it.
"""

import pytest

from .test_drive_gear import streamer
from .test_streamers_api import app_for, write_config

FLASH_URL = "ws://flash.example:8001"  # made up: no flash service is ever called


@pytest.fixture
def flash_on(monkeypatch):
    monkeypatch.setenv("FLASH_ACTIONS_URL", FLASH_URL)


@pytest.fixture
def flash_off(monkeypatch):
    monkeypatch.delenv("FLASH_ACTIONS_URL", raising=False)


def patch(client, sid="youtube:@a", **body):
    return client.patch("/api/streamers", params={"id": sid}, json=body)


def test_ticking_the_flash_box_is_kept(tmp_path, flash_on):
    cfg = write_config(tmp_path)
    client = app_for(cfg)

    assert client.get("/api/streamers").json()["flash_available"] is True
    assert patch(client, flash=True).status_code == 200

    restarted = app_for(cfg)
    assert streamer(restarted, "youtube:@a")["flash"] is True
    assert streamer(restarted, "twitch:b")["flash"] is False


def test_without_a_flash_service_the_box_cannot_be_ticked(tmp_path, flash_off):
    cfg = write_config(tmp_path)
    client = app_for(cfg)

    assert client.get("/api/streamers").json()["flash_available"] is False
    r = patch(client, flash=True, auto_download=False)

    assert r.status_code == 422
    assert "闪传" in r.json()["detail"]
    a = streamer(app_for(cfg), "youtube:@a")  # nothing of the request saved
    assert (a["flash"], a["auto_download"]) == (False, True)


def test_unticking_is_allowed_without_a_flash_service(tmp_path, flash_off):
    cfg = write_config(tmp_path, streamers=[
        {"name": "A", "platform": "youtube", "channel": "@a", "flash": True},  # ticked while one was set
    ])
    client = app_for(cfg)

    assert patch(client, flash=True, keep=3).status_code == 200  # saving the gear as it stands
    assert streamer(app_for(cfg), "youtube:@a")["keep"] == 3

    assert patch(client, flash=False).status_code == 200
    assert streamer(app_for(cfg), "youtube:@a")["flash"] is False


def test_the_setting_is_per_entry_and_reaches_the_running_pipeline(tmp_path, flash_on):
    cfg = write_config(tmp_path, streamers=[
        {"name": "森野鈴", "platform": "youtube", "channel": "@suzu"},
        {"name": "森野鈴", "platform": "twitch", "channel": "suzu"},  # the same name, another entry
    ])
    client = app_for(cfg)
    auto = client.app.state.auto

    assert patch(client, "youtube:@suzu", flash=True).status_code == 200

    assert auto.flash_streamers == {"youtube:@suzu"}
    assert streamer(client, "twitch:suzu")["flash"] is False
    assert app_for(cfg).app.state.auto.flash_streamers == {"youtube:@suzu"}  # at start too


def test_the_flash_box_is_independent_of_the_drive_box(tmp_path, flash_on):
    client = app_for(write_config(tmp_path))
    auto = client.app.state.auto

    assert patch(client, drive=True).status_code == 200
    assert (auto.drive_folders, auto.flash_streamers) == ({"youtube:@a": "录播/A"}, set())

    assert patch(client, flash=True).status_code == 200
    assert (auto.drive_folders, auto.flash_streamers) == ({"youtube:@a": "录播/A"}, {"youtube:@a"})

    assert patch(client, drive=False).status_code == 200
    assert (auto.drive_folders, auto.flash_streamers) == ({}, {"youtube:@a"})

    assert patch(client, flash=False).status_code == 200
    assert (auto.drive_folders, auto.flash_streamers) == ({}, set())
